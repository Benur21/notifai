#!/usr/bin/env python3
"""
NotifAI — sistema de vigilância com notificações.

Pensado para correr via cron a cada minuto. Lê config.json, descobre que
jobs estão em atraso para hoje, corre cada um (pesquisa via Tavily + análise
via Gemma local no Pi), e notifica via ntfy.sh quando aplicável.

Falhas são registadas em log local — nunca geram notificação de erro — e
nunca bloqueiam os outros jobs nem impedem tentativas futuras (a próxima
execução agendada tenta sempre do zero).
"""

import fcntl
import json
import os
import time
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests

BASE_DIR = Path(__file__).resolve().parent


def carregar_env(caminho: Path) -> dict:
    """Lê um ficheiro .env simples (uma variável por linha, KEY=VALOR).
    Linhas vazias ou a começar por # são ignoradas. Não faz parsing
    sofisticado de propósito — não precisamos de mais do que isto."""
    valores = {}
    if not caminho.exists():
        return valores
    with open(caminho, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            valores[chave.strip()] = valor.strip().strip('"').strip("'")
    return valores


# Variáveis de ambiente reais (ex: definidas no systemd) têm sempre
# prioridade sobre o ficheiro .env, que serve sobretudo para correres
# localmente sem teres de exportar nada à mão.
_env = carregar_env(BASE_DIR / ".env")


def obter_segredo(nome: str) -> str:
    return os.environ.get(nome) or _env.get(nome, "")


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Segredos — vêm do .env (ou de variáveis de ambiente reais), nunca daqui
TAVILY_API_KEY = obter_segredo("TAVILY_API_KEY")
NTFY_TOPIC = obter_segredo("NTFY_TOPIC")

# Logs detalhados por fase (chamadas, durações) — úteis para depurar, mas
# escrevem mais vezes no cartão SD. Põe LOG_VERBOSE=true no .env se quiseres.
LOG_VERBOSE = obter_segredo("LOG_VERBOSE").strip().lower() in ("1", "true", "sim", "yes")

# Não-segredos — estes podem continuar no código sem problema
TAVILY_URL = "https://api.tavily.com/search"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODELO_GEMMA = "gemma3:1b"
NTFY_URL = "https://ntfy.sh"

# Dias de tolerância após a data prevista de um evento antes de assumirmos
# "provavelmente já aconteceu" mesmo sem confirmação clara nas notícias.
DIAS_TOLERANCIA_DATA = 3

LIMITE_CARATERES_NOTIFICACAO = 2800

# ============================================================

CONFIG_PATH = BASE_DIR / "config.json"
ESTADO_AGENDAMENTO_PATH = BASE_DIR / "estado_agendamento.json"
ESTADO_CONTEUDO_PATH = BASE_DIR / "estado_conteudo.json"
LOG_PATH = BASE_DIR / "notifai.log"
LOCK_PATH = BASE_DIR / "notifai.lock"


# ---------- utilitários básicos ----------

def log(mensagem: str) -> None:
    linha = f"[{datetime.now().isoformat(timespec='seconds')}] {mensagem}"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def log_debug(mensagem: str) -> None:
    """Como log(), mas só escreve se LOG_VERBOSE estiver ligado — para não
    estar sempre a gravar no cartão SD em uso normal."""
    if LOG_VERBOSE:
        log(mensagem)


def carregar_json(caminho: Path, default):
    if not caminho.exists():
        return default
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as erro:
        log(f"AVISO: falha a ler {caminho.name}, a usar default. Erro: {erro}")
        return default


def guardar_json(caminho: Path, dados) -> None:
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)


def adquirir_lock():
    """Tenta obter um trinco exclusivo. Devolve o ficheiro aberto (a manter
    vivo durante toda a execução) se conseguir, ou None se já houver outra
    execução em curso — para evitar execuções sobrepostas quando um job
    demora mais do que o intervalo do cron."""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except BlockingIOError:
        lock_file.close()
        return None


def validar_configuracao() -> bool:
    problemas = []
    if not TAVILY_API_KEY:
        problemas.append("TAVILY_API_KEY não encontrada (.env ou variável de ambiente).")
    if not NTFY_TOPIC:
        problemas.append("NTFY_TOPIC não encontrado (.env ou variável de ambiente).")
    for p in problemas:
        log(f"CONFIG INVÁLIDA: {p}")
    return not problemas


# ---------- agendamento ----------

def jobs_em_atraso(config: list, estado_agendamento: dict):
    """Devolve lista de (job, horario) que já passaram da hora marcada e
    ainda não correram hoje."""
    agora = datetime.now()
    hoje_str = agora.date().isoformat()
    em_atraso = []

    for job in config:
        job_id = job["id"]
        for horario in job.get("horarios", []):
            hora_agendada = datetime.strptime(horario, "%H:%M").time()
            ja_correu_hoje = estado_agendamento.get(job_id, {}).get(horario) == hoje_str

            if agora.time() >= hora_agendada and not ja_correu_hoje:
                em_atraso.append((job, horario))

    return em_atraso


def marcar_corrido(estado_agendamento: dict, job_id: str, horario: str) -> None:
    estado_agendamento.setdefault(job_id, {})[horario] = date.today().isoformat()


# ---------- integrações externas ----------

def pesquisar_tavily(query: str, time_range: str = "month") -> dict:
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "include_answer": True,
        "max_results": 5,
        "time_range": time_range,
    }
    resp = requests.post(TAVILY_URL, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def resumir_resultados_tavily(resultado_tavily: dict, max_caracteres_por_resultado: int = 500) -> dict:
    """Reduz a resposta crua da Tavily ao essencial antes de ir para o
    prompt do Gemma — menos ruído, menos tokens, menos risco de estourar
    o contexto e cortar o JSON de resposta a meio."""
    resumo = {"resposta_sintetizada": resultado_tavily.get("answer", "")}
    resultados_reduzidos = []
    for r in resultado_tavily.get("results", [])[:5]:
        resultados_reduzidos.append({
            "titulo": r.get("title", ""),
            "url": r.get("url", ""),
            "data_publicacao": r.get("published_date", ""),
            "conteudo": (r.get("content", "") or "")[:max_caracteres_por_resultado],
        })
    resumo["resultados"] = resultados_reduzidos
    return resumo


# Schema real (não só "é JSON válido") — restringe a geração aos nomes e
# tipos de campo exatos, em vez de confiar que o modelo escreve "resumo"
# e não "resuma". Usado por todos os jobs, mesmo os que não usam todos os
# campos (ex: estado_lancamento só interessa a jobs de eventos agendados).
SCHEMA_RESPOSTA_GEMMA = {
    "type": "object",
    "properties": {
        "ha_novidade": {"type": "boolean"},
        "titulo": {"type": "string"},
        "novidades": {"type": "string"},
        "resumo_completo": {"type": "string"},
        "estado_lancamento": {"type": "string"},
        "data_lancamento": {"type": ["string", "null"]},
        "fontes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "ha_novidade",
        "titulo",
        "novidades",
        "resumo_completo",
        "estado_lancamento",
        "data_lancamento",
        "fontes",
    ],
}


def perguntar_gemma(prompt: str) -> dict:
    """Chama o Gemma local e devolve o JSON já parseado, com os campos
    restringidos pelo schema (não apenas "é JSON válido")."""
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODELO_GEMMA,
            "prompt": prompt,
            "stream": False,
            "format": SCHEMA_RESPOSTA_GEMMA,
            "options": {
                "temperature": 0.2,
                "num_ctx": 8192,     # o defeito do Ollama (2048-4096) corta o JSON a meio em prompts maiores
                "num_predict": 800,  # suficiente para o schema, evita gerações descontroladas
            },
        },
        timeout=240,
    )
    resp.raise_for_status()
    texto = resp.json().get("response", "")
    return json.loads(texto)  # propaga JSONDecodeError se o conteúdo não bater certo


def notificar_ntfy(titulo: str, mensagem: str, fontes: list) -> None:
    if len(mensagem) > LIMITE_CARATERES_NOTIFICACAO:
        mensagem = mensagem[:LIMITE_CARATERES_NOTIFICACAO] + "\n\n(...cortado)"

    payload = {
        "topic": NTFY_TOPIC,
        "title": titulo,
        "message": mensagem,
        "tags": ["rocket"],
    }

    fontes_validas = [f for f in (fontes or []) if f]
    if fontes_validas:
        payload["click"] = fontes_validas[0]  # tocar na notificação abre a fonte principal
        payload["actions"] = [
            {"action": "view", "label": f"Fonte {i + 2}", "url": url}
            for i, url in enumerate(fontes_validas[1:4])  # até 3 botões extra
        ]

    resp = requests.post(NTFY_URL, json=payload, timeout=15)
    resp.raise_for_status()


# ---------- lógica de cada job ----------

def nota_data_passada(estado_anterior: dict) -> str:
    """Rede de segurança: se a data prevista já passou há vários dias sem
    confirmação, avisa o Gemma para verificar se o evento já ocorreu."""
    data_str = estado_anterior.get("data_lancamento") if estado_anterior else None
    if not data_str or estado_anterior.get("estado_lancamento") == "lancado":
        return ""

    try:
        data_prevista = datetime.strptime(data_str, "%Y-%m-%d").date()
    except ValueError:
        return ""

    if date.today() - data_prevista > timedelta(days=DIAS_TOLERANCIA_DATA):
        return (
            f"\n\nNota: a data anteriormente prevista ({data_str}) já passou há mais de "
            f"{DIAS_TOLERANCIA_DATA} dias sem confirmação clara. Verifica nos resultados "
            "se entretanto já ocorreu, e se sim, atualiza para o evento seguinte."
        )
    return ""


def processar_job(job: dict) -> Optional[dict]:
    """Corre um job e devolve o novo estado de conteúdo, ou None se falhou."""
    job_id = job["id"]
    estado_anterior = ESTADO_CONTEUDO_ATUAL.get(job_id, {})

    log_debug(f"[{job_id}] a chamar Tavily...")
    t0 = time.time()
    try:
        resultado_tavily = pesquisar_tavily(
            job["promptTavily"], time_range=job.get("tavily_time_range", "month")
        )
    except Exception as erro:
        log(f"ERRO [{job_id}] Tavily: {erro}")
        return None
    log_debug(f"[{job_id}] Tavily respondeu em {time.time() - t0:.1f}s "
              f"({len(resultado_tavily.get('results', []))} resultados)")

    resultados_resumidos = resumir_resultados_tavily(resultado_tavily)
    log_debug(f"[{job_id}] Tavily resumo:\n{json.dumps(resultados_resumidos, indent=2, ensure_ascii=False)}")

    prompt_final = job["promptZAI"]
    prompt_final = prompt_final.replace(
        "{{ESTADO_ANTERIOR}}", json.dumps(estado_anterior, ensure_ascii=False)
    )
    prompt_final = prompt_final.replace(
        "{{RESULTADOS_TAVILY}}", json.dumps(resultados_resumidos, ensure_ascii=False)
    )
    prompt_final = prompt_final.replace("{{DATA_HOJE}}", date.today().isoformat())
    prompt_final += nota_data_passada(estado_anterior)

    log_debug(f"[{job_id}] a chamar Gemma...")
    t0 = time.time()
    try:
        resposta = perguntar_gemma(prompt_final)
    except Exception as erro:
        log(f"ERRO [{job_id}] Gemma: {erro}")
        return None
    log_debug(f"[{job_id}] Gemma respondeu em {time.time() - t0:.1f}s")
    log_debug(f"[{job_id}] Gemma resposta:\n{json.dumps(resposta, indent=2, ensure_ascii=False)}")

    campos_esperados = {"ha_novidade", "titulo", "novidades", "resumo_completo", "fontes"}
    if not campos_esperados.issubset(resposta.keys()):
        log(f"ERRO [{job_id}] resposta do Gemma sem os campos esperados: {resposta}")
        return None

    modo = job.get("modo", "novidade")
    deve_notificar = modo == "diario" or resposta.get("ha_novidade") is True
    log_debug(f"[{job_id}] modo={modo} ha_novidade={resposta.get('ha_novidade')} "
              f"-> {'vai notificar' if deve_notificar else 'não notifica'}")

    if deve_notificar:
        usar_delta = modo == "novidade" and resposta.get("novidades")
        corpo = resposta["novidades"] if usar_delta else resposta["resumo_completo"]
        try:
            notificar_ntfy(resposta["titulo"], corpo, resposta.get("fontes", []))
            log_debug(f"[{job_id}] notificação enviada.")
        except Exception as erro:
            log(f"ERRO [{job_id}] ntfy: {erro}")
    else:
        log(f"[{job_id}] sem novidade, não notificou.")

    return resposta


# ---------- main ----------

ESTADO_CONTEUDO_ATUAL: dict = {}


def main() -> None:
    global ESTADO_CONTEUDO_ATUAL

    if not validar_configuracao():
        return  # já registado em log por validar_configuracao()

    lock = adquirir_lock()
    if lock is None:
        log("Já existe uma execução em curso — a sair sem fazer nada.")
        return

    try:
        config = carregar_json(CONFIG_PATH, [])
        estado_agendamento = carregar_json(ESTADO_AGENDAMENTO_PATH, {})
        ESTADO_CONTEUDO_ATUAL = carregar_json(ESTADO_CONTEUDO_PATH, {})

        pendentes = jobs_em_atraso(config, estado_agendamento)

        # Agrupa por job_id: se dois horários do MESMO job estiverem em
        # atraso ao mesmo tempo (ex: Pi esteve desligado e ambos os
        # horários de hoje já passaram), o trabalho (Tavily + Gemma) só é
        # feito uma vez — o conteúdo seria igual de qualquer forma.
        por_job = defaultdict(list)
        for job, horario in pendentes:
            por_job[job["id"]].append((job, horario))

        for job_id, ocorrencias in por_job.items():
            job = ocorrencias[0][0]
            horarios_pendentes = [h for _, h in ocorrencias]
            log(f"--- A processar job '{job_id}' (horários em atraso: {', '.join(horarios_pendentes)}) ---")

            try:
                novo_estado = processar_job(job)
                if novo_estado is not None:
                    ESTADO_CONTEUDO_ATUAL[job_id] = novo_estado
            except Exception as erro:
                log(f"ERRO inesperado [{job_id}]: {erro}")
            finally:
                # marca TODOS os horários em atraso como tentados hoje —
                # evita martelar a Tavily/Gemma a cada minuto; só volta a
                # tentar no próximo horário agendado
                for horario in horarios_pendentes:
                    marcar_corrido(estado_agendamento, job_id, horario)

        guardar_json(ESTADO_AGENDAMENTO_PATH, estado_agendamento)
        guardar_json(ESTADO_CONTEUDO_PATH, ESTADO_CONTEUDO_ATUAL)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()