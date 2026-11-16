import os
import time
import json
import math
import logging
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Tuple, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ==============================================================================
# 1. LOGS E VALIDAÇÃO DE AMBIENTE
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY]):
    raise ValueError("ERRO CRÍTICO: Verifique TELEGRAM_TOKEN, TELEGRAM_CHAT_ID e GEMINI_API_KEY no ambiente.")

try:
    BANCA_TOTAL = float(os.getenv("BANCA_TOTAL", "1000"))
    RISCO_POR_OPERACAO = float(os.getenv("RISCO_POR_OPERACAO", "0.025"))  # Padrão campeão: 2.5%
    if BANCA_TOTAL <= 0 or not (0.001 <= RISCO_POR_OPERACAO <= 0.10):
        raise ValueError("BANCA_TOTAL deve ser maior que 0 e RISCO_POR_OPERACAO entre 0.001 e 0.10.")
except ValueError as e:
    raise ValueError(f"ERRO DE CONFIGURAÇÃO DE RISCO: {e}")

# ==============================================================================
# 2. PARÂMETROS EXATOS DO MOTOR CAMPEÃO (CENÁRIO 4: +253.13% EM 5 ANOS)
# ==============================================================================
PARES = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "LINK/USDT"]
TIMEFRAME = "4h"
MULTIPLICADOR_ATR = 3.0
RELACAO_RISCO_RETORNO = 2.5
MAX_STOP_PERCENT = 0.08
MIN_STOP_PERCENT = 0.005
MAX_POSICOES_SIMULTANEAS = 2       # Regra Cenário 4: máximo 2 vagas abertas
COOLDOWN_ALERTA_SEGUNDOS = 28800   # 8 horas sem repetir alerta da mesma moeda
FUSO_BRASILIA = timezone(timedelta(hours=-3))

FEEDS_RSS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
]


# ==============================================================================
# 3. SESSÃO HTTP BLINDADA E CLIENTE IA
# ==============================================================================
def criar_sessao_http() -> requests.Session:
    sessao = requests.Session()
    estrategia_retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adaptador = HTTPAdapter(max_retries=estrategia_retry)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    sessao.headers.update({"User-Agent": "BotConselheiroCripto/3.4"})
    return sessao


http_session = criar_sessao_http()
ai_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta")
)

ultimo_alerta = {}
posicoes_abertas = set()           # Ex: {"BTC/USDT", "SOL/USDT"}
ultimo_dia_boletim = ""
trava_memoria = threading.Lock()


# ==============================================================================
# 4. SERVIDOR HEALTH-CHECK (RENDER + UPTIMEROBOT)
# ==============================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot Conselheiro v3.4 (Breakout 50 - Max 2 Pos) Online")

    def log_message(self, format, *args):
        return


def iniciar_servidor_healthcheck() -> None:
    porta = int(os.getenv("PORT", "10000"))
    servidor = HTTPServer(("0.0.0.0", porta), HealthCheckHandler)
    logging.info(f"Servidor Health-Check ativo na porta {porta}")
    servidor.serve_forever()


# ==============================================================================
# 5. SCHEMA ESTRITO PARA A IA
# ==============================================================================
class AnaliseRiscoIA(BaseModel):
    vetar_operacao: bool = Field(
        description="True APENAS se houver notícia grave/catastrófica (hack, falência, processo SEC, bug crítico)."
    )
    nivel_risco: str = Field(
        description="Estritamente: BAIXO, MEDIO ou ALTO"
    )
    resumo_noticias: str = Field(
        description="Resumo objetivo em português (máximo 2 frases) do cenário atual."
    )


# ==============================================================================
# 6. COLETA DE DADOS (ENDPOINT GLOBAL SEM BLOQUEIO 451)
# ==============================================================================
def buscar_velas_binance(par: str, intervalo: str = "4h", limite: int = 250) -> list:
    simbolo = par.replace("/", "").upper()
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": simbolo, "interval": intervalo, "limit": limite}
    resp = http_session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    dados = resp.json()

    if not isinstance(dados, list) or len(dados) < 205:
        raise ValueError(f"Histórico insuficiente retornado para {par}")

    return [
        [int(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4]), float(v[5])]
        for v in dados
    ]


def buscar_fear_and_greed() -> str:
    try:
        resp = http_session.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        resp.raise_for_status()
        dado = resp.json()["data"][0]
        return f"{dado['value']}/100 ({dado['value_classification']})"
    except Exception as e:
        logging.warning(f"Fear & Greed indisponível: {e}")
        return "Indisponível"


def coletar_manchetes(limite_por_feed: int = 8) -> List[str]:
    manchetes = []
    for url in FEEDS_RSS:
        try:
            resp = http_session.get(url, timeout=12)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:limite_por_feed]:
                titulo = entry.get("title", "").strip()
                if titulo:
                    manchetes.append(f"- {titulo}")
        except Exception as e:
            logging.warning(f"Falha ao ler feed RSS ({url}): {e}")
    return manchetes


def obter_modelos_ativos() -> List[str]:
    candidatos = [
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-2.5-flash",
    ]
    try:
        for m in ai_client.models.list():
            nome = getattr(m, "name", "").replace("models/", "")
            acoes = getattr(m, "supported_actions", []) or []
            if "gemini" in nome and "flash" in nome and "generateContent" in acoes:
                if nome not in candidatos:
                    candidatos.append(nome)
    except Exception as e:
        logging.warning(f"Não foi possível listar modelos dinamicamente: {e}")
    return candidatos


def consultar_ia_veto(par: str, preco: float, contexto: str) -> Tuple[bool, str, str]:
    manchetes = coletar_manchetes()
    if not manchetes:
        return False, "RSS OFFLINE", "Não foi possível carregar notícias. Valide manualmente antes de entrar."

    texto_manchetes = "\n".join(manchetes[:16])
    prompt = f"""
    Você é um analista de risco quantitativo de criptomoedas.
    O sistema técnico detectou um rompimento de alta ({contexto}) para {par} (Preço: ${preco:.4f}).
    Verifique nas manchetes abaixo se há algum evento catastrófico (hack, golpe, falência, processo regulatório grave) acontecendo AGORA que justifique VETAR a compra.
    Volatilidade normal de mercado sem notícia grave NÃO deve ser vetada.

    Manchetes recentes:
    {texto_manchetes}
    """

    modelos_candidatos = obter_modelos_ativos()
    ultimo_erro = ""

    for modelo in modelos_candidatos:
        try:
            response = ai_client.models.generate_content(
                model=modelo,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AnaliseRiscoIA,
                    temperature=0.1,
                ),
            )
            if not response or not response.text:
                raise ValueError("Resposta vazia da API.")

            resultado = json.loads(response.text)
            nivel = str(resultado.get("nivel_risco", "MEDIO")).upper()
            if nivel not in ("BAIXO", "MEDIO", "ALTO"):
                nivel = "MEDIO"

            logging.info(f"Análise IA concluída usando: {modelo}")
            return (
                bool(resultado.get("vetar_operacao", False)),
                nivel,
                str(resultado.get("resumo_noticias", "Sem resumo disponível."))
            )
        except Exception as e:
            ultimo_erro = str(e)
            logging.warning(f"Falha no modelo {modelo}: {e}")

    logging.error(f"Todos os modelos de IA falharam. Último erro: {ultimo_erro}")
    return False, "IA OFFLINE", "Erro temporário na IA. Valide notícias manualmente antes de operar."


# ==============================================================================
# 7. INDICADORES TÉCNICOS E GESTÃO DE RISCO
# ==============================================================================
def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    delta = df["close"].diff()
    ganho = delta.clip(lower=0)
    perda = -delta.clip(upper=0)

    media_ganho = ganho.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    media_perda = perda.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = media_ganho / media_perda.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["max_50"] = df["high"].rolling(window=50).max().shift(1)
    df["vol_medio"] = df["volume"].rolling(window=20).mean()
    return df


def calcular_tamanho_mao(stop_percentual: float, banca_custom: Optional[float] = None) -> Tuple[float, float]:
    banca_base = banca_custom if banca_custom is not None else BANCA_TOTAL
    stop_seguro = max(stop_percentual, MIN_STOP_PERCENT)
    risco_maximo_valor = banca_base * RISCO_POR_OPERACAO
    tamanho_posicao = risco_maximo_valor / stop_seguro
    tamanho_posicao = min(tamanho_posicao, banca_base * 0.40)
    return tamanho_posicao, risco_maximo_valor


def normalizar_par(texto_moeda: str) -> Optional[str]:
    moeda = texto_moeda.upper().replace("/USDT", "").replace("USDT", "").strip()
    if not moeda.isalnum() or len(moeda) > 10:
        return None
    return f"{moeda}/USDT"


# ==============================================================================
# 8. MOTOR BREAKOUT 50 VELAS + RADAR + CALCULADORA
# ==============================================================================
def obter_dados_par(par: str) -> Optional[dict]:
    velas = buscar_velas_binance(par, intervalo=TIMEFRAME, limite=250)
    df = pd.DataFrame(velas, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = calcular_indicadores(df)

    v1 = df.iloc[-2]  # Última vela fechada
    v0 = df.iloc[-1]  # Vela atual

    preco_atual = float(v0["close"])
    fechamento_v1 = float(v1["close"])
    atr = float(v1["atr"])
    ema200 = float(v1["ema_200"])
    rsi_v1 = float(v1["rsi"])
    max_50 = float(v1["max_50"])
    vol_v1 = float(v1["volume"])
    vol_medio = float(v1["vol_medio"])

    valores = [preco_atual, fechamento_v1, atr, ema200, rsi_v1, max_50, vol_v1, vol_medio]
    if any(not math.isfinite(x) for x in valores) or preco_atual <= 0 or atr <= 0:
        return None

    acima_ema200 = preco_atual > ema200
    rompeu_topo50 = fechamento_v1 > max_50 and vol_v1 > vol_medio
    dist_rompimento = ((preco_atual - max_50) / max_50) * 100

    distancia_stop = atr * MULTIPLICADOR_ATR
    stop_loss = preco_atual - distancia_stop
    stop_percentual = distancia_stop / preco_atual
    take_profit = preco_atual + (distancia_stop * RELACAO_RISCO_RETORNO)
    alvo_percentual = stop_percentual * RELACAO_RISCO_RETORNO

    return {
        "par": par,
        "preco": preco_atual,
        "ema200": ema200,
        "rsi": rsi_v1,
        "max_50": max_50,
        "acima_ema200": acima_ema200,
        "sinal_ativo": acima_ema200 and rompeu_topo50,
        "dist_rompimento": dist_rompimento,
        "stop_loss": stop_loss,
        "stop_pct": stop_percentual,
        "take_profit": take_profit,
        "alvo_pct": alvo_percentual,
    }


def gerar_relatorio_radar(titulo: str = "📡 <b>RADAR BREAKOUT 50v (4h)</b>") -> str:
    linhas = [titulo]
    with trava_memoria:
        vagas_usadas = len(posicoes_abertas)
        lista_abertas = ", ".join(sorted(posicoes_abertas)) if posicoes_abertas else "Nenhuma"

    fng = buscar_fear_and_greed()
    linhas.append(f"• Banca: <code>{BANCA_TOTAL:.2f} USDT</code> | Vagas: <code>{vagas_usadas}/{MAX_POSICOES_SIMULTANEAS}</code> ({lista_abertas})")
    linhas.append(f"• Fear & Greed: <code>{fng}</code>\n")

    for par in PARES:
        try:
            d = obter_dados_par(par)
            if not d:
                linhas.append(f"• <b>{par}</b>: ⚠️ Dados indisponíveis")
                continue

            if d["sinal_ativo"]:
                status = "🚨 <b>ROMPENDO AGORA!</b>"
            elif d["acima_ema200"]:
                status = f"🟢 Alta | Falta <code>{abs(d['dist_rompimento']):.2f}%</code> p/ topo (<code>${d['max_50']:.2f}</code>)"
            else:
                status = f"🔴 Baixa (Abaixo EMA 200 | <code>{d['dist_rompimento']:+.2f}%</code>)"

            linhas.append(f"• <b>{par.replace('/USDT', '')}</b> (<code>${d['preco']:.2f}</code>): {status}")
            time.sleep(0.3)
        except Exception as e:
            logging.warning(f"Erro no radar para {par}: {e}")
            linhas.append(f"• <b>{par.replace('/USDT', '')}</b>: ⚠️ Erro de leitura")

    linhas.append("\n💡 <i>Use /analisar MOEDA para ver Stop, Alvo e Tamanho de Mão.</i>")
    return "\n".join(linhas)


def analisar_par(par: str, forcar_relatorio: bool = False, banca_custom: Optional[float] = None) -> Optional[str]:
    try:
        d = obter_dados_par(par)
        if not d:
            logging.warning(f"[{par}] Valores matemáticos inválidos ignorados.")
            return None

        banca_ref = banca_custom if banca_custom is not None else BANCA_TOTAL
        tamanho_mao, perda_maxima = calcular_tamanho_mao(d["stop_pct"], banca_ref)

        with trava_memoria:
            vagas_usadas = len(posicoes_abertas)
            ja_aberta = par in posicoes_abertas

        if forcar_relatorio:
            if banca_custom is not None:
                return (
                    f"🧮 <b>CALCULADORA DE ORDEM OCO: {par}</b>\n\n"
                    f"• Banca simulada: <code>{banca_ref:.2f} USDT</code> (Risco {RISCO_POR_OPERACAO*100:.1f}%)\n"
                    f"• Preço Atual: <code>${d['preco']:.4f}</code>\n"
                    f"• Alocar na ordem: <code>{tamanho_mao:.2f} USDT</code>\n"
                    f"• Perda máx. no Stop: <code>{perda_maxima:.2f} USDT</code>\n"
                    f"• Stop Loss (3x ATR): <code>${d['stop_loss']:.4f}</code> (-{d['stop_pct']*100:.2f}%)\n"
                    f"• Take Profit (1:2.5): <code>${d['take_profit']:.4f}</code> (+{d['alvo_pct']*100:.2f}%)"
                )

            _, nivel_risco, resumo_ia = consultar_ia_veto(par, d["preco"], "Consulta Manual")
            fng = buscar_fear_and_greed()
            tendencia_txt = "🟢 ALTA (Acima da EMA 200)" if d["acima_ema200"] else "🔴 BAIXA (Abaixo da EMA 200)"
            status_txt = (
                "🚨 <b>ROMPIMENTO ATIVO AGORA</b>"
                if d["sinal_ativo"]
                else f"⚪ Aguardando romper topo 50v (<code>${d['max_50']:.4f}</code> | {d['dist_rompimento']:+.2f}%)"
            )

            return (
                f"🔍 <b>RAIO-X SOB DEMANDA: {par} ({TIMEFRAME})</b>\n\n"
                f"• Preço: <code>${d['preco']:.4f}</code>\n"
                f"• Tendência Macro: {tendencia_txt}\n"
                f"• Topo 50 Velas: <code>${d['max_50']:.4f}</code> | RSI: <code>{d['rsi']:.1f}</code>\n"
                f"• Gatilho Breakout: {status_txt}\n"
                f"• Vagas em uso: <code>{vagas_usadas}/{MAX_POSICOES_SIMULTANEAS}</code> | Fear & Greed: <code>{fng}</code>\n\n"
                f"🧠 <b>Contexto IA ({nivel_risco}):</b>\n{resumo_ia}\n\n"
                f"🛡️ <b>Gestão de Risco (Banca: {banca_ref:.2f} USDT | Risco {RISCO_POR_OPERACAO*100:.1f}%):</b>\n"
                f"• Entrada sugerida: <code>{tamanho_mao:.2f} USDT</code> (Perda máx: {perda_maxima:.2f} USDT)\n"
                f"• Stop Loss (3x ATR): <code>${d['stop_loss']:.4f}</code> (-{d['stop_pct']*100:.2f}%)\n"
                f"• Take Profit (1:2.5): <code>${d['take_profit']:.4f}</code> (+{d['alvo_pct']*100:.2f}%)"
            )

        if d["sinal_ativo"]:
            agora = time.time()
            with trava_memoria:
                if par in ultimo_alerta and (agora - ultimo_alerta[par]) < COOLDOWN_ALERTA_SEGUNDOS:
                    return None

            if d["stop_loss"] <= 0 or not (MIN_STOP_PERCENT <= d["stop_pct"] <= MAX_STOP_PERCENT):
                logging.info(f"[{par}] Sinal descartado por Stop fora da faixa ({d['stop_pct']*100:.2f}%).")
                return None

            logging.info(f"[{par}] Breakout 50 velas detectado! Consultando IA...")
            vetar, nivel_risco, resumo_ia = consultar_ia_veto(par, d["preco"], "Rompimento de 50 Velas")

            with trava_memoria:
                ultimo_alerta[par] = agora

            if vetar:
                enviar_telegram(
                    f"🛑 <b>SINAL VETADO PELA IA: {par}</b>\n"
                    f"• Gatilho: <b>Rompimento de 50 Velas (4h)</b>\n"
                    f"• <b>Motivo do Veto:</b> {resumo_ia}"
                )
                return None

            aviso_vagas = ""
            if ja_aberta:
                aviso_vagas = f"\n⚠️ <b>ATENÇÃO:</b> Você já marcou <b>{par}</b> como aberta em /posicoes."
            elif vagas_usadas >= MAX_POSICOES_SIMULTANEAS:
                aviso_vagas = (
                    f"\n⚠️ <b>REGRA CENÁRIO 4 (2/2 VAGAS CHEIAS):</b> Você já está com {vagas_usadas} posições abertas. "
                    f"Pelo backtest de 5 anos (+253%), recomenda-se <b>ignorar</b> este 3º alerta."
                )
            else:
                aviso_vagas = f"\n✅ <b>Vaga disponível ({vagas_usadas}/{MAX_POSICOES_SIMULTANEAS}):</b> Após comprar, envie <code>/abrir {par.replace('/USDT', '')}</code>."

            fng = buscar_fear_and_greed()
            mensagem = (
                f"🚨 <b>OPORTUNIDADE APROVADA: {par}</b>\n\n"
                f"🚀 <b>Estratégia ({TIMEFRAME}):</b> Breakout 50 Velas\n"
                f"• Preço Atual: <code>${d['preco']:.4f}</code> (Rompeu <code>${d['max_50']:.4f}</code>)\n"
                f"• EMA 200: <code>${d['ema200']:.4f}</code> | RSI: <code>{d['rsi']:.1f}</code>\n"
                f"• Fear & Greed: <code>{fng}</code>\n\n"
                f"🧠 <b>Filtro IA ({nivel_risco}):</b>\n"
                f"• {resumo_ia}\n\n"
                f"💰 <b>Tamanho de Mão (Banca {banca_ref:.2f} USDT):</b>\n"
                f"• Alocar na ordem: <code>{tamanho_mao:.2f} USDT</code>\n"
                f"• Perda máx. no Stop: <code>{perda_maxima:.2f} USDT</code> ({RISCO_POR_OPERACAO*100:.1f}%)\n\n"
                f"🛡️ <b>Ordem OCO (1:{RELACAO_RISCO_RETORNO:g}):</b>\n"
                f"• Stop Loss: <code>${d['stop_loss']:.4f}</code> (-{d['stop_pct']*100:.2f}%)\n"
                f"• Take Profit: <code>${d['take_profit']:.4f}</code> (+{d['alvo_pct']*100:.2f}%)"
                f"{aviso_vagas}"
            )
            enviar_telegram(mensagem)
            logging.info(f"[{par}] Alerta Breakout 50 enviado com sucesso!")

    except requests.RequestException as e:
        logging.warning(f"[{par}] Falha temporária de rede na Binance: {e}")
    except Exception as e:
        logging.error(f"[{par}] Erro inesperado ao analisar par: {e}", exc_info=True)
    return None


# ==============================================================================
# 9. TELEGRAM (ENVIO E ESCUTA DE COMANDOS OPERACIONAIS)
# ==============================================================================
def enviar_telegram(mensagem: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}
    try:
        resp = http_session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Falha ao enviar mensagem no Telegram: {e}")


def escutar_comandos_telegram() -> None:
    global BANCA_TOTAL
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = 0

    while True:
        try:
            resp = http_session.get(url, params={"timeout": 25, "offset": offset}, timeout=35)
            resp.raise_for_status()
            dados = resp.json()

            for item in dados.get("result", []):
                offset = item["update_id"] + 1
                msg = item.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                texto = msg.get("text", "").strip()

                if chat_id != TELEGRAM_CHAT_ID or not texto:
                    continue

                cmd = texto.lower().split()[0]

                if cmd in ("/start", "/ajuda", "/help"):
                    enviar_telegram(
                        "🤖 <b>COMANDOS DO BOT CONSELHEIRO v3.4</b>\n\n"
                        "• <code>/status</code> — Saúde do bot, banca e vagas\n"
                        "• <code>/radar</code> — Distância das 5 moedas até o rompimento\n"
                        "• <code>/analisar BTC</code> — Raio-X completo + IA de uma moeda\n"
                        "• <code>/calc SOL 200</code> — Simula ordem OCO para banca customizada\n"
                        "• <code>/posicoes</code> — Ver vagas abertas (máx. 2 simultâneas)\n"
                        "• <code>/abrir BTC</code> — Registra posição aberta na Binance\n"
                        "• <code>/fechar BTC</code> — Libera vaga após bater Stop ou Alvo\n"
                        "• <code>/banca 140</code> — Atualiza banca em USDT na hora"
                    )

                elif cmd == "/status":
                    fng = buscar_fear_and_greed()
                    with trava_memoria:
                        vagas = len(posicoes_abertas)
                        abertas_txt = ", ".join(sorted(posicoes_abertas)) if posicoes_abertas else "Nenhuma"
                    enviar_telegram(
                        f"✅ <b>Bot Conselheiro v3.4 Online (Cenário 4)</b>\n"
                        f"• Estratégia: <code>Breakout 50v + EMA 200 (4h)</code>\n"
                        f"• Banca atual: <code>{BANCA_TOTAL:.2f} USDT</code> (Risco: {RISCO_POR_OPERACAO*100:.1f}%)\n"
                        f"• Vagas em uso: <code>{vagas}/{MAX_POSICOES_SIMULTANEAS}</code> ({abertas_txt})\n"
                        f"• Boletim diário: <code>09:00 (Brasília)</code>\n"
                        f"• Fear & Greed: <code>{fng}</code>\n\n"
                        f"💡 <i>Envie /ajuda para listar todos os comandos.</i>"
                    )

                elif cmd == "/radar":
                    enviar_telegram("⏳ Varrendo as 5 moedas líderes (BTC, ETH, SOL, BNB, LINK)...")
                    enviar_telegram(gerar_relatorio_radar())

                elif cmd in ("/posicoes", "/vagas"):
                    with trava_memoria:
                        vagas = len(posicoes_abertas)
                        abertas_txt = "\n".join([f"  • <code>{p}</code>" for p in sorted(posicoes_abertas)]) if posicoes_abertas else "  • <i>Nenhuma posição aberta (2 vagas livres)</i>"
                    enviar_telegram(
                        f"📂 <b>CONTROLE DE VAGAS ({vagas}/{MAX_POSICOES_SIMULTANEAS})</b>\n\n"
                        f"{abertas_txt}\n\n"
                        f"• Para registrar entrada: <code>/abrir MOEDA</code>\n"
                        f"• Para liberar vaga: <code>/fechar MOEDA</code>"
                    )

                elif cmd == "/abrir":
                    partes = texto.split()
                    par_alvo = normalizar_par(partes[1]) if len(partes) >= 2 else None
                    if not par_alvo:
                        enviar_telegram("⚠️ Exemplo: <code>/abrir BTC</code>")
                        continue
                    with trava_memoria:
                        if par_alvo in posicoes_abertas:
                            enviar_telegram(f"ℹ️ <b>{par_alvo}</b> já está registrada como aberta ({len(posicoes_abertas)}/{MAX_POSICOES_SIMULTANEAS}).")
                        elif len(posicoes_abertas) >= MAX_POSICOES_SIMULTANEAS:
                            enviar_telegram(
                                f"🚫 <b>LIMITE ATINGIDO ({MAX_POSICOES_SIMULTANEAS}/{MAX_POSICOES_SIMULTANEAS})!</b>\n"
                                f"Pela regra do Cenário 4, encerre uma posição existente (<code>/fechar MOEDA</code>) antes de abrir <b>{par_alvo}</b>."
                            )
                        else:
                            posicoes_abertas.add(par_alvo)
                            enviar_telegram(f"✅ Posição <b>{par_alvo}</b> registrada! Vagas em uso: <code>{len(posicoes_abertas)}/{MAX_POSICOES_SIMULTANEAS}</code>.")

                elif cmd == "/fechar":
                    partes = texto.split()
                    par_alvo = normalizar_par(partes[1]) if len(partes) >= 2 else None
                    if not par_alvo:
                        enviar_telegram("⚠️ Exemplo: <code>/fechar BTC</code>")
                        continue
                    with trava_memoria:
                        if par_alvo in posicoes_abertas:
                            posicoes_abertas.remove(par_alvo)
                            enviar_telegram(f"🔓 Posição <b>{par_alvo}</b> encerrada! Vagas em uso: <code>{len(posicoes_abertas)}/{MAX_POSICOES_SIMULTANEAS}</code>.")
                        else:
                            enviar_telegram(f"ℹ️ <b>{par_alvo}</b> não estava na lista de posições abertas.")

                elif cmd == "/banca":
                    partes = texto.split()
                    if len(partes) < 2:
                        enviar_telegram(f"💰 Banca atual: <code>{BANCA_TOTAL:.2f} USDT</code>\nPara alterar: <code>/banca 140</code>")
                        continue
                    try:
                        novo_valor = float(partes[1].replace(",", "."))
                        if novo_valor <= 0:
                            raise ValueError()
                        BANCA_TOTAL = novo_valor
                        enviar_telegram(
                            f"💰 <b>Banca atualizada na memória para {BANCA_TOTAL:.2f} USDT!</b>\n\n"
                            f"⚠️ <i>Lembrete: atualize também a variável <code>BANCA_TOTAL</code> no painel do Render para não resetar caso o servidor reinicie.</i>"
                        )
                    except ValueError:
                        enviar_telegram("⚠️ Valor inválido. Exemplo: <code>/banca 140</code>")

                elif cmd in ("/calc", "/calculadora"):
                    partes = texto.split()
                    par_consulta = normalizar_par(partes[1]) if len(partes) >= 2 else None
                    if not par_consulta:
                        enviar_telegram("⚠️ Exemplo: <code>/calc SOL</code> ou <code>/calc SOL 200</code>")
                        continue
                    banca_sim = None
                    if len(partes) >= 3:
                        try:
                            banca_sim = float(partes[2].replace(",", "."))
                        except ValueError:
                            enviar_telegram("⚠️ Valor de banca inválido. Exemplo: <code>/calc SOL 200</code>")
                            continue
                    relatorio = analisar_par(par_consulta, forcar_relatorio=True, banca_custom=banca_sim or BANCA_TOTAL)
                    if relatorio:
                        enviar_telegram(relatorio)
                    else:
                        enviar_telegram(f"❌ Não encontrei dados válidos para <b>{par_consulta}</b>.")

                elif cmd == "/analisar":
                    partes = texto.split()
                    par_consulta = normalizar_par(partes[1]) if len(partes) >= 2 else None
                    if not par_consulta:
                        enviar_telegram("⚠️ Exemplo: <code>/analisar BTC</code> ou <code>/analisar SOL</code>")
                        continue
                    enviar_telegram(f"⏳ Analisando <b>{par_consulta}</b> (Breakout 50v + EMA200 + IA)...")
                    relatorio = analisar_par(par_consulta, forcar_relatorio=True)
                    if relatorio:
                        enviar_telegram(relatorio)
                    else:
                        enviar_telegram(f"❌ Não encontrei dados válidos para <b>{par_consulta}</b>.")

        except Exception as e:
            logging.debug(f"Ciclo de polling Telegram: {e}")
            time.sleep(5)


# ==============================================================================
# 10. LOOP PRINCIPAL + BOLETIM DIÁRIO AUTOMÁTICO (09:00 BRASÍLIA)
# ==============================================================================
if __name__ == "__main__":
    logging.info("Iniciando Bot Conselheiro v3.4 (Breakout 50 - Cenário 4)...")

    thread_web = threading.Thread(target=iniciar_servidor_healthcheck, daemon=True)
    thread_web.start()

    enviar_telegram(
        "🤖 <b>Bot Conselheiro v3.4 Ativo!</b>\n"
        f"• Motor Campeão: <b>Breakout 50v (4h | Máx {MAX_POSICOES_SIMULTANEAS} vagas)</b>\n"
        f"• Banca: <b>{BANCA_TOTAL:.2f} USDT</b> | Risco: <b>{RISCO_POR_OPERACAO*100:.1f}%</b>\n"
        "• Digite <code>/ajuda</code> para ver todos os comandos."
    )

    thread_telegram = threading.Thread(target=escutar_comandos_telegram, daemon=True)
    thread_telegram.start()

    while True:
        try:
            agora_br = datetime.now(FUSO_BRASILIA)
            hoje_str = agora_br.strftime("%Y-%m-%d")

            # Envia o boletim diário automaticamente a partir das 09:00 (Brasília)
            if agora_br.hour == 9 and ultimo_dia_boletim != hoje_str:
                ultimo_dia_boletim = hoje_str
                logging.info("Disparando boletim diário das 09:00...")
                enviar_telegram(gerar_relatorio_radar("☀️ <b>BOLETIM DIÁRIO (09:00) — RADAR 4h</b>"))

            for par in PARES:
                analisar_par(par, forcar_relatorio=False)
                time.sleep(1.5)

            time.sleep(300)
        except KeyboardInterrupt:
            logging.info("Bot encerrado manualmente.")
            break
        except Exception as e:
            logging.critical(f"Erro no loop principal: {e}. Reiniciando em 60s...", exc_info=True)
            time.sleep(60)