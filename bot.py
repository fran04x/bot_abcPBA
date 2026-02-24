import os
import ssl
import html
import requests
import urllib3
import time
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# --- CONFIGURACIÓN ---
CUIL = os.environ.get("CUIL")
PASSWORD = os.environ.get("PASSWORD")
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INSECURE_SSL = os.environ.get("INSECURE_SSL", "false").strip().lower() in {"1", "true", "yes"}
REQUEST_TIMEOUT = (10, 30)

# Credenciales de Upstash Redis
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

try:
    TELEGRAM_MAX_MESSAGE_LEN = int(os.environ.get("TELEGRAM_MAX_MESSAGE_LEN", "4096"))
    if TELEGRAM_MAX_MESSAGE_LEN < 1:
        raise ValueError("TELEGRAM_MAX_MESSAGE_LEN debe ser mayor a 0")
except ValueError:
    TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_MESSAGE_LEN = min(TELEGRAM_MAX_MESSAGE_LEN, 4096)

if INSECURE_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Activo")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"[*] Servidor web escuchando en puerto {port}...", flush=True)
    server.serve_forever()

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        if INSECURE_SSL:
            context = create_urllib3_context()
            # Bajamos el nivel de seguridad a 0 y permitimos todos los cifrados
            context.set_ciphers("ALL:@SECLEVEL=0")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            # Esta línea fuerza a Python a aceptar conexiones con servidores antiguos
            if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
                context.options |= ssl.OP_LEGACY_SERVER_CONNECT
            kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def enviar_telegram(mensaje, silencioso=False):
    if not TOKEN or not CHAT_ID:
        print("[-] TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no configurados.", flush=True)
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    def enviar_parte(texto):
        payload = {
            "chat_id": CHAT_ID,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silencioso
        }

        payload_plain = {
            "chat_id": CHAT_ID,
            "text": texto,
            "disable_web_page_preview": True,
            "disable_notification": silencioso
        }

        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            detalle = None
            if getattr(error, "response", None) is not None:
                try:
                    detalle = error.response.json().get("description")
                except (ValueError, TypeError, KeyError):
                    detalle = error.response.text

            if status is not None and 400 <= status < 500:
                print(f"[!] Error HTML en Telegram ({status}): {detalle}. Reintentando en texto plano...", flush=True)
                try:
                    plain_response = requests.post(url, json=payload_plain, timeout=REQUEST_TIMEOUT)
                    plain_response.raise_for_status()
                    print("[*] Mensaje enviado en texto plano tras fallback.", flush=True)
                    return
                except requests.RequestException as fallback_error:
                    print(f"[-] Error enviando Telegram (fallback texto plano): {fallback_error}", flush=True)
                    return

            print(f"[-] Error enviando Telegram: {error}", flush=True)

    max_len = TELEGRAM_MAX_MESSAGE_LEN
    if len(mensaje) <= max_len:
        enviar_parte(mensaje)
        return

    print(f"[!] Mensaje largo ({len(mensaje)} chars). Fragmentando en partes <= {max_len}.", flush=True)
    partes = []
    bloque = ""
    for linea in mensaje.splitlines(keepends=True):
        if len(linea) > max_len:
            if bloque:
                partes.append(bloque)
                bloque = ""
            inicio = 0
            while inicio < len(linea):
                partes.append(linea[inicio:inicio + max_len])
                inicio += max_len
            continue

        if len(bloque) + len(linea) <= max_len:
            bloque += linea
        else:
            partes.append(bloque)
            bloque = linea

    if bloque:
        partes.append(bloque)

    for idx, parte in enumerate(partes, start=1):
        if len(partes) > 1:
            encabezado = f"<i>Parte {idx}/{len(partes)}</i>\n"
            if len(encabezado) + len(parte) <= max_len:
                enviar_parte(encabezado + parte)
            else:
                enviar_parte(parte)
        else:
            enviar_parte(parte)

def obtener_top_postulantes(session, id_oferta):
    url_p = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "3", "wt": "json"}
    try:
        r = session.get(url_p, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            if not docs: return "<i>Sin postulantes aún</i>"
            res = ""
            for i, p in enumerate(docs, 1):
                nombre = html.escape(f"{p.get('apellido', '')} {p.get('nombre', '')}".title().strip())
                puntaje = html.escape(str(p.get('puntaje', '0.00')))
                res += f"  {i}º {nombre} | <b>{puntaje} pts</b>\n"
            return res
    except requests.RequestException:
        return "<i>Error en ranking</i>"
    except (ValueError, KeyError, TypeError):
        return "<i>Error parseando ranking</i>"
    return "<i>Sin datos</i>"

def monitorear():
    print("[*] Monitoreo silencioso iniciado con base de datos Upstash...", flush=True)
    ofertas_avisadas_local = set() # Backup local por si falla internet
    buffer_ofertas = []
    ultimo_turno_enviado = None

    tz_ar = timezone(timedelta(hours=-3))
    
    while True:
        try:
            with requests.Session() as session:
                session.mount('https://', TLSAdapter())
                session.headers.update({'User-Agent': 'Mozilla/5.0'})

                login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
                payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
                login_response = session.post(login_url, data=payload, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
                if login_response.status_code != 200:
                    print(f"[!] Login con estado inesperado: {login_response.status_code}", flush=True)

                url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
                params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Designada"', "rows": "1000", "wt": "json"}
                r = session.get(url_solr, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
            
                if r.status_code == 200:
                    docs = r.json().get("response", {}).get("docs", [])
                    hallazgos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo","")).upper()]
                    
                    ts = int(time.time() * 1000)

                    for info in hallazgos:
                        id_o = info.get('idoferta')
                        es_nueva = False

                        # LÓGICA DE MEMORIA EN LA NUBE CON UPSTASH
                        if UPSTASH_URL and UPSTASH_TOKEN:
                            base_url = UPSTASH_URL.rstrip('/')
                            # Usamos el comando SADD (Set Add) de Redis vía REST
                            url_redis = f"{base_url}/sadd/ofertas_enviadas/{id_o}"
                            headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
                            try:
                                resp = requests.get(url_redis, headers=headers, timeout=5)
                                if resp.status_code == 200:
                                    # La API devuelve 1 si se agregó (es nuevo), o 0 si ya estaba
                                    if resp.json().get("result") == 1:
                                        es_nueva = True
                            except Exception as e:
                                print(f"[-] Error conectando a Redis, usando RAM local: {e}", flush=True)
                                # Fallback local
                                if id_o not in ofertas_avisadas_local:
                                    ofertas_avisadas_local.add(id_o)
                                    es_nueva = True
                        else:
                            # Fallback si no pusiste las variables en Render
                            if id_o not in ofertas_avisadas_local:
                                ofertas_avisadas_local.add(id_o)
                                es_nueva = True

                        if es_nueva:
                            ranking = obtener_top_postulantes(session, id_o)
                            link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"
                            escuela = html.escape(str(info.get('escuela', '')))
                            cargo = html.escape(str(info.get('cargo', '')))
                            link_escapado = html.escape(link, quote=True)
                            
                            texto_oferta = f"🏫 <b>Escuela:</b> {escuela}\n"
                            texto_oferta += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                            texto_oferta += f"🏆 <b>Top 3:</b>\n{ranking}"
                            texto_oferta += f"🔗 <a href=\"{link_escapado}\">VER ESCUELA</a>\n"
                            texto_oferta += "───────────────────\n"

                            buffer_ofertas.append(texto_oferta)

                    ahora = datetime.now(tz_ar)
                    hora_actual = ahora.hour

                    es_hora_de_envio = (hora_actual == 9 and ultimo_turno_enviado != 9) or (hora_actual == 21 and ultimo_turno_enviado != 21)

                    if es_hora_de_envio:
                        hora_str = ahora.strftime("%H:%M")
                        
                        if not buffer_ofertas:
                            enviar_telegram(f"⏳ <i>{hora_str} hs - Información actualizada: Sin cargos nuevos.</i>", silencioso=True)
                        else:
                            bloque = ""
                            contador = 0
                            for txt in buffer_ofertas:
                                bloque += txt
                                contador += 1
                                if contador >= 15:
                                    enviar_telegram(f"🚨 <b>RESUMEN DE CARGOS ({hora_str} hs)</b> 🚨\n\n{bloque}")
                                    bloque = ""
                                    contador = 0
                                    time.sleep(2)
                                    
                            if bloque:
                                enviar_telegram(f"🚨 <b>RESUMEN DE CARGOS ({hora_str} hs)</b> 🚨\n\n{bloque}")

                        buffer_ofertas.clear()

                    ultimo_turno_enviado = hora_actual
                else:
                    print(f"[!] Consulta de ofertas devolvió estado {r.status_code}", flush=True)

            print(f"[*] Revisión oculta finalizada. Ofertas en espera: {len(buffer_ofertas)}", flush=True)
        except requests.RequestException as error:
            print(f"[-] Error de red: {error}", flush=True)
        except Exception as e:
            print(f"[-] Error: {e}", flush=True)
        
        time.sleep(900)

if __name__ == "__main__":
    faltantes = [
        key for key, value in {
            "CUIL": CUIL,
            "PASSWORD": PASSWORD,
            "TELEGRAM_TOKEN": TOKEN,
            "TELEGRAM_CHAT_ID": CHAT_ID,
        }.items() if not value
    ]
    if faltantes:
        raise RuntimeError(f"Faltan variables de entorno obligatorias: {', '.join(faltantes)}")

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    time.sleep(5)
    monitorear()
