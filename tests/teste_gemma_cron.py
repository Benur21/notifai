#!/usr/bin/env python3
"""
Script de teste isolado, pensado para correr via cron no Raspberry Pi.

Testa DUAS coisas ao mesmo tempo:
1. Se o cron está mesmo a disparar o script à hora marcada.
2. Se o Gemma 4 (via Ollama, local) responde corretamente quando chamado
   de forma não-interativa (sem terminal aberto à frente).

Os resultados ficam num ficheiro de log que podes abrir via VNC
(terminal: `cat ~/teste_gemma/log.txt` ou `tail -f ~/teste_gemma/log.txt`).
"""

import time
from datetime import datetime
from pathlib import Path

import requests

LOG_PATH = Path.home() / "teste_gemma" / "log.txt"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODELO = "gemma4:e2b"
PROMPT_TESTE = "Em uma frase, o que é um Raspberry Pi?"


def escrever_log(linha: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def main() -> None:
    inicio = datetime.now()
    escrever_log(f"--- Execução em {inicio.isoformat()} ---")

    try:
        t0 = time.time()
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODELO, "prompt": PROMPT_TESTE, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        duracao = time.time() - t0

        escrever_log(f"OK — respondeu em {duracao:.1f}s")
        escrever_log(f"Resposta: {data.get('response', '').strip()}")
    except Exception as erro:
        escrever_log(f"ERRO: {erro}")

    escrever_log("--- Fim ---\n")


if __name__ == "__main__":
    main()
