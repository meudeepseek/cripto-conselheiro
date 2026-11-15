import os
import time
import json
import math
import logging
import threading
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
# 1. LOGS E VALIDAÇÃO RIGOROSA DE AMBIENTE
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
    raise ValueError("ERRO CRÍTICO: Verifique TELEGRAM_TOKEN, TELEGRAM_CHAT_ID e GEMINI_API_KEY no .env")

try:
    BANCA_TOTAL = float(os.getenv("BANCA_TOTAL", "1000"))
    RISCO_POR_OPERACAO = float(os.getenv("RISCO_POR_OPERACAO", "0.015"))
    if BANCA_TOTAL <= 0 or not (0.001 <= RISCO_POR_OPERACAO <= 0.10):
        raise ValueError("BANCA_TOTAL deve ser > 0 e RISCO_POR_OPERACAO entre 0.001 (0.1%) e 0.10 (10%).")
except ValueError as e:
    raise ValueError(f"ERRO DE CONFIGURAÇÃO NO .ENV: {e}")

# ==============================================================================
# 2. PARÂMETROS DA ESTRATÉGIA
# ==============================================================================
PARES = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "LINK/USDT"]
TIMEFRAME = "1h"
RSI_LIMITE_COMPRA = 30.0
MULTIPLICADOR_ATR = 1.5
MAX_STOP_PERCENT = 0.045       # Teto máximo de 4.5% de Stop Loss
MIN_STOP_PERCENT = 0.004       # Piso mínimo de 0.4% (evita divisão por números minúsculos)
RELACAO_RISCO_RETORNO = 2.0    # Alvo sempre 2x o tamanho do Stop (1:2)
COOLDOWN_ALERTA_SEGUNDOS = 14400  # 4 horas sem repetir alerta da mesma moeda

FEEDS_RSS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
]

# ==============================================================================
# 3. SESSÃO HTTP BLINDADA (RETENTATIVAS AUTOMÁTICAS) E CLIENTE IA
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
    sessao.headers.update({"User-Agent": "BotConselheiroCripto/3.0"})
    return sessao

http_session = criar_sessao_http()
ai_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta")
)

ultimo_alerta = {}
trava_memoria = threading.Lock()


# ==============================================================================
# 4. SCHEMA ESTRITO PARA A IA (CONTRA ALUCINAÇÃO)
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
# 5. COLETA DE DADOS (BINANCE, RSS E FEAR & GREED)
# ==============================================================================
def buscar_velas_binance(par: str, intervalo: str = "1h", limite: int = 100) -> list:
    simbolo = par.replace("/", "").upper()
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": simbolo, "interval": intervalo, "limit": limite}
    resp = http_session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    dados = resp.json()

    if not isinstance(dados, list) or len(dados) < 30:
        raise ValueError(f"Resposta inválida ou curta da Binance para {par}")

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
    """Consulta a API do Google e retorna os modelos Gemini ativos que suportam geração de texto."""
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


def consultar_ia_veto(par: str, preco: float, rsi: float) -> Tuple[bool, str, str]:
    manchetes = coletar_manchetes()
    if not manchetes:
        return False, "RSS OFFLINE", "Não foi possível carregar notícias. Valide manualmente antes de entrar."

    texto_manchetes = "\n".join(manchetes[:16])
    prompt = f"""
    Você é um analista de risco quantitativo de criptomoedas.
    O sistema técnico detectou uma possível compra para {par} (Preço: ${preco:.4f}, RSI: {rsi:.1f}).
    Verifique nas manchetes abaixo se há algum evento catastrófico (hack, golpe, falência, processo regulatório grave) acontecendo AGORA que justifique VETAR a compra.
    Quedas normais de mercado sem notícia grave NÃO devem ser vetadas.

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

            logging.info(f"Análise IA concluída com sucesso usando o modelo: {modelo}")
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
    manchetes = coletar_manchetes()
    if not manchetes:
        return False, "RSS OFFLINE", "Não foi possível carregar notícias. Valide manualmente antes de entrar."

    texto_manchetes = "\n".join(manchetes[:16])
    prompt = f"""
    Você é um analista de risco quantitativo de criptomoedas.
    O sistema técnico detectou uma possível compra para {par} (Preço: ${preco:.4f}, RSI: {rsi:.1f}).
    Verifique nas manchetes abaixo se há algum evento catastrófico (hack, golpe, falência, processo regulatório grave) acontecendo AGORA que justifique VETAR a compra.
    Quedas normais de mercado sem notícia grave NÃO devem ser vetadas.

    Manchetes recentes:
    {texto_manchetes}
    """

    modelos_candidatos = [
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-3.1-pro-preview",
    ]
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
    manchetes = coletar_manchetes()
    if not manchetes:
        return False, "RSS OFFLINE", "Não foi possível carregar notícias. Valide manualmente antes de entrar."

    texto_manchetes = "\n".join(manchetes[:16])
    prompt = f"""
    Você é um analista de risco quantitativo de criptomoedas.
    O sistema técnico detectou uma possível compra para {par} (Preço: ${preco:.4f}, RSI: {rsi:.1f}).
    Verifique nas manchetes abaixo se há algum evento catastrófico (hack, golpe, falência, processo regulatório grave) acontecendo AGORA que justifique VETAR a compra.
    Quedas normais de mercado sem notícia grave NÃO devem ser vetadas.

    Manchetes recentes:
    {texto_manchetes}
    """

    modelos_disponiveis = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    ultimo_erro = ""

    for modelo in modelos_disponiveis:
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
    manchetes = coletar_manchetes()
    if not manchetes:
        return False, "RSS OFFLINE", "Não foi possível carregar notícias. Valide manualmente antes de entrar."

    texto_manchetes = "\n".join(manchetes[:16])
    prompt = f"""
    Você é um analista de risco quantitativo de criptomoedas.
    O sistema técnico detectou uma possível compra para {par} (Preço: ${preco:.4f}, RSI: {rsi:.1f}).
    Verifique nas manchetes abaixo se há algum evento catastrófico (hack, golpe, falência, processo regulatório grave) acontecendo AGORA que justifique VETAR a compra.
    Quedas normais de mercado sem notícia grave NÃO devem ser vetadas.

    Manchetes recentes:
    {texto_manchetes}
    """

    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnaliseRiscoIA,
                temperature=0.1,
            ),
        )
        if not response or not response.text:
            raise ValueError("Resposta vazia da API do Gemini.")

        resultado = json.loads(response.text)
        nivel = str(resultado.get("nivel_risco", "MEDIO")).upper()
        if nivel not in ("BAIXO", "MEDIO", "ALTO"):
            nivel = "MEDIO"

        return (
            bool(resultado.get("vetar_operacao", False)),
            nivel,
            str(resultado.get("resumo_noticias", "Sem resumo disponível."))
        )
    except Exception as e:
        logging.error(f"Falha na consulta à IA: {e}")
        return False, "IA OFFLINE", "Erro temporário na IA. Valide notícias manualmente antes de operar."


# ==============================================================================
# 6. CÁLCULOS TÉCNICOS E GESTÃO DE RISCO
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
    df["vol_medio"] = df["volume"].rolling(window=20).mean()
    return df


def calcular_tamanho_mao(stop_percentual: float) -> Tuple[float, float]:
    stop_seguro = max(stop_percentual, MIN_STOP_PERCENT)
    risco_maximo_valor = BANCA_TOTAL * RISCO_POR_OPERACAO
    tamanho_posicao = risco_maximo_valor / stop_seguro
    # Trava de segurança: nunca sugerir alocar mais de 40% da banca em uma única moeda
    tamanho_posicao = min(tamanho_posicao, BANCA_TOTAL * 0.40)
    return tamanho_posicao, risco_maximo_valor


# ==============================================================================
# 7. MOTOR DE ANÁLISE (AUTOMÁTICO E SOB DEMANDA)
# ==============================================================================
def analisar_par(par: str, forcar_relatorio: bool = False) -> Optional[str]:
    try:
        velas = buscar_velas_binance(par, intervalo=TIMEFRAME, limite=100)
        df = pd.DataFrame(velas, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = calcular_indicadores(df)

        vela_fechada = df.iloc[-2]
        preco_atual = float(df.iloc[-1]["close"])
        rsi = float(vela_fechada["rsi"])
        atr = float(vela_fechada["atr"])
        volume_atual = float(vela_fechada["volume"])
        volume_medio = float(vela_fechada["vol_medio"])

        # Validação rigorosa contra NaN / Infinito / Zero
        valores_criticos = [preco_atual, rsi, atr, volume_atual, volume_medio]
        if any(not math.isfinite(v) for v in valores_criticos) or preco_atual <= 0 or atr <= 0:
            logging.warning(f"[{par}] Valores matemáticos inválidos ignorados.")
            return None

        distancia_stop = atr * MULTIPLICADOR_ATR
        stop_loss = preco_atual - distancia_stop
        stop_percentual = distancia_stop / preco_atual

        if stop_loss <= 0 or stop_percentual < MIN_STOP_PERCENT:
            logging.warning(f"[{par}] Stop Loss inválido ou curto demais ({stop_percentual*100:.2f}%).")
            return None

        take_profit = preco_atual + (distancia_stop * RELACAO_RISCO_RETORNO)
        alvo_percentual = stop_percentual * RELACAO_RISCO_RETORNO
        tamanho_mao, perda_maxima = calcular_tamanho_mao(stop_percentual)

        # Modo Sob Demanda (/analisar)
        if forcar_relatorio:
            _, nivel_risco, resumo_ia = consultar_ia_veto(par, preco_atual, rsi)
            fng = buscar_fear_and_greed()
            status_rsi = "🟢 SOBREVENDIDO (Ponto de compra)" if rsi < RSI_LIMITE_COMPRA else "⚪ NEUTRO (Aguardar)"
            aviso_stop = " ⚠️ <i>(Stop acima do teto de 4.5%)</i>" if stop_percentual > MAX_STOP_PERCENT else ""

            return (
                f"🔍 <b>RAIO-X SOB DEMANDA: {par}</b>\n\n"
                f"• Preço: <code>${preco_atual:.4f}</code>\n"
                f"• RSI (14): <code>{rsi:.1f}</code> — {status_rsi}\n"
                f"• Fear & Greed: <code>{fng}</code>\n\n"
                f"🧠 <b>Contexto IA ({nivel_risco}):</b>\n{resumo_ia}\n\n"
                f"🛡️ <b>Gestão de Risco (Banca: {BANCA_TOTAL:.0f}):</b>\n"
                f"• Entrada sugerida: <code>{tamanho_mao:.2f}</code> (Perda máx: {perda_maxima:.2f})\n"
                f"• Stop Loss: <code>${stop_loss:.4f}</code> (-{stop_percentual*100:.2f}%){aviso_stop}\n"
                f"• Take Profit: <code>${take_profit:.4f}</code> (+{alvo_percentual*100:.2f}%)"
            )

        # Modo Automático
        if rsi < RSI_LIMITE_COMPRA and volume_atual > (volume_medio * 0.8):
            agora = time.time()
            with trava_memoria:
                if par in ultimo_alerta and (agora - ultimo_alerta[par]) < COOLDOWN_ALERTA_SEGUNDOS:
                    return None

            if stop_percentual > MAX_STOP_PERCENT:
                logging.info(f"[{par}] Descartado: Stop técnico muito longo ({stop_percentual*100:.2f}%)")
                return None

            logging.info(f"[{par}] Sinal técnico detectado (RSI: {rsi:.1f}). Consultando IA...")
            vetar, nivel_risco, resumo_ia = consultar_ia_veto(par, preco_atual, rsi)

            with trava_memoria:
                ultimo_alerta[par] = agora

            if vetar:
                enviar_telegram(
                    f"🛑 <b>SINAL VETADO PELA IA: {par}</b>\n"
                    f"• RSI em <code>{rsi:.1f}</code>, mas a IA detectou risco elevado.\n"
                    f"• <b>Motivo:</b> {resumo_ia}"
                )
                return None

            fng = buscar_fear_and_greed()
            mensagem = (
                f"🚨 <b>OPORTUNIDADE APROVADA: {par}</b>\n\n"
                f"📊 <b>Dados Técnicos ({TIMEFRAME}):</b>\n"
                f"• Preço Atual: <code>${preco_atual:.4f}</code>\n"
                f"• RSI (14): <code>{rsi:.1f}</code>\n"
                f"• Fear & Greed: <code>{fng}</code>\n\n"
                f"🧠 <b>Filtro IA ({nivel_risco}):</b>\n"
                f"• {resumo_ia}\n\n"
                f"💰 <b>Tamanho de Mão (Banca {BANCA_TOTAL:.0f}):</b>\n"
                f"• Alocar na ordem: <code>{tamanho_mao:.2f}</code>\n"
                f"• Perda máx. no Stop: <code>{perda_maxima:.2f}</code> ({RISCO_POR_OPERACAO*100:.1f}%)\n\n"
                f"🛡️ <b>Ordem OCO (1:2):</b>\n"
                f"• Stop Loss: <code>${stop_loss:.4f}</code> (-{stop_percentual*100:.2f}%)\n"
                f"• Take Profit: <code>${take_profit:.4f}</code> (+{alvo_percentual*100:.2f}%)"
            )
            enviar_telegram(mensagem)
            logging.info(f"[{par}] Alerta aprovado enviado com sucesso!")

    except requests.RequestException as e:
        logging.warning(f"[{par}] Falha temporária de rede na Binance: {e}")
    except Exception as e:
        logging.error(f"[{par}] Erro inesperado ao analisar par: {e}", exc_info=True)
    return None


# ==============================================================================
# 8. COMUNICAÇÃO E ESCUTA DE COMANDOS NO TELEGRAM
# ==============================================================================
def enviar_telegram(mensagem: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}
    try:
        resp = http_session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Falha definitiva ao enviar mensagem no Telegram: {e}")


def escutar_comandos_telegram() -> None:
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

                # Bloqueio de segurança: só responde ao dono do TELEGRAM_CHAT_ID
                if chat_id != TELEGRAM_CHAT_ID or not texto:
                    continue

                if texto.lower() == "/status":
                    fng = buscar_fear_and_greed()
                    enviar_telegram(
                        f"✅ <b>Bot Conselheiro v3 Online</b>\n"
                        f"• Banca Configurada: <code>{BANCA_TOTAL:.2f}</code> (Risco: {RISCO_POR_OPERACAO*100:.1f}%)\n"
                        f"• Pares: <code>{', '.join(PARES)}</code>\n"
                        f"• Timeframe: <code>{TIMEFRAME}</code> | RSI Alvo: <code>&lt; {RSI_LIMITE_COMPRA}</code>\n"
                        f"• Fear & Greed: <code>{fng}</code>\n\n"
                        f"💡 <i>Envie /analisar BTC para consultar qualquer moeda na hora.</i>"
                    )

                elif texto.lower().startswith("/analisar"):
                    partes = texto.split()
                    if len(partes) < 2:
                        enviar_telegram("⚠️ Formato inválido. Exemplo: <code>/analisar BTC</code> ou <code>/analisar SOL</code>")
                        continue
                    moeda = partes[1].upper().replace("/USDT", "").replace("USDT", "")
                    if not moeda.isalnum() or len(moeda) > 10:
                        enviar_telegram("⚠️ Símbolo de moeda inválido.")
                        continue

                    par_consulta = f"{moeda}/USDT"
                    enviar_telegram(f"⏳ Analisando <b>{par_consulta}</b> (gráfico + notícias + IA)...")
                    relatorio = analisar_par(par_consulta, forcar_relatorio=True)
                    if relatorio:
                        enviar_telegram(relatorio)
                    else:
                        enviar_telegram(f"❌ Não encontrei dados válidos para <b>{par_consulta}</b> na Binance.")

        except Exception as e:
            logging.debug(f"Ciclo de polling Telegram: {e}")
            time.sleep(5)


# ==============================================================================
# 9. LOOP PRINCIPAL
# ==============================================================================
if __name__ == "__main__":
    logging.info("Iniciando Bot Conselheiro v3 (Produção Blindada)...")
    enviar_telegram(
        "🤖 <b>Bot Conselheiro v3 Iniciado!</b>\n"
        "Comandos disponíveis:\n"
        "• <code>/status</code> — Verifica saúde do bot e humor do mercado\n"
        "• <code>/analisar BTC</code> — Faz raio-X imediato de qualquer moeda"
    )

    thread_telegram = threading.Thread(target=escutar_comandos_telegram, daemon=True)
    thread_telegram.start()

    while True:
        try:
            for par in PARES:
                analisar_par(par, forcar_relatorio=False)
                time.sleep(1.5)
            time.sleep(300)
        except KeyboardInterrupt:
            logging.info("Bot encerrado manualmente pelo usuário.")
            break
        except Exception as e:
            logging.critical(f"Erro crítico no loop principal: {e}. Reiniciando ciclo em 60s...", exc_info=True)
            time.sleep(60)