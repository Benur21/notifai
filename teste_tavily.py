#!/usr/bin/env python3
"""
Script de teste isolado para a API da Tavily.

Faz UMA pesquisa e guarda a resposta crua num ficheiro JSON para
inspecionares a estrutura real antes de desenharmos o parsing definitivo.

Corre isto manualmente uma vez (não precisa de cron):
    python3 teste_tavily.py
"""

import json
import requests

# Cola aqui a tua chave (começa por "tvly-")
TAVILY_API_KEY = "tvly-A_TUA_CHAVE_AQUI"

TAVILY_URL = "https://api.tavily.com/search"

# Mesma query (ou parecida) à que vamos usar a sério no projeto
QUERY = "next SpaceX Starship test flight launch date"

payload = {
    "api_key": TAVILY_API_KEY,
    "query": QUERY,
    "search_depth": "advanced",
    "include_answer": True,
    "max_results": 5,
}

print("A chamar a Tavily...")
resp = requests.post(TAVILY_URL, json=payload, timeout=30)
resp.raise_for_status()
data = resp.json()

with open("tavily_resposta_teste.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print("Resposta guardada em tavily_resposta_teste.json")
print(f"Número de resultados: {len(data.get('results', []))}")
if "answer" in data and data["answer"]:
    print(f"Resposta sintetizada (preview): {data['answer'][:200]}...")
