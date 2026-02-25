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
        raise ValueError()
except ValueError:
    TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_MESSAGE_LEN = min(TELEGRAM_MAX_MESSAGE_LEN, 4096)

if INSECURE_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot con Memoria de Estados Activo")
        
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"[*] Servidor web escuchando en puerto {port}...", flush=True)
    server.serve_forever()

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        if INSECURE_SSL:
            context = create_urllib3_context()
            context.set_ciphers("ALL:@SECLEVEL=0")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
                context.options |= ssl.OP_LEGACY_SERVER_CONNECT
            kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def enviar_telegram(mensaje, silencioso=False):
    if not TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    def enviar_parte(texto):
        payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": silencioso}
        payload_plain = {"chat_id": CHAT_ID, "text": texto, "disable_web_page_preview": True, "disable_notification": silencioso}

        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                try:
                    requests.post(url, json=payload_plain, timeout=REQUEST_TIMEOUT).raise_for_status()
                    return
                except:
                    pass
            print(f"[-] Error enviando Telegram: {error}", flush=True)

    max_len = TELEGRAM_MAX_MESSAGE_LEN
    if len(mensaje) <= max_len:
        enviar_parte(mensaje)
        return

    partes = []
    bloque = ""
    for linea in mensaje.splitlines(keepends=True):
        if len(bloque) + len(linea) <= max_len:
            bloque += linea
        else:
            partes.append(bloque)
            bloque = linea
    if bloque:
        partes.append(bloque)

    for idx, parte in enumerate(partes, start=1):
        enviar_parte(parte)

def obtener_top_postulantes(session, id_oferta):
    url_p = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "10", "wt": "json"}
    try:
        r = session.get(url_p, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            if not docs: return "<i>Sin postulantes aún</i>"
            res = ""
            activos_mostrados = 0
            for p in docs:
                estado_post = str(p.get('estadopostulacion', '')).upper()
                designado = str(p.get('designado', '')).upper()
                if estado_post != "ACTIVA" or designado in ["S", "Y"]:
                    continue
                nombre = html.escape(f"{p.get('apellido', '')} {p.get('nombre', '')}".title().strip())
                res += f"  {activos_mostrados + 1}º {nombre} | <b>{html.escape(str(p.get('puntaje', '0.00')))} pts</b>\n"
                activos_mostrados += 1
                if activos_mostrados >= 3: break
            if activos_mostrados == 0: return "<i>Postulantes inactivos (¡Vía libre!)</i>"
            return res
    except:
        return "<i>Error en ranking</i>"
    return "<i>Sin datos</i>"

def monitorear():
    print("[*] Monitoreo inteligente con máquina de estados iniciado...", flush=True)
    # Reemplazamos el set() por un diccionario para guardar {id: estado}
    ofertas_estados_local = {} 
    
    HORAS_REPORTE = {6, 9, 12, 15, 17, 20, 20, 22}
    ultimo_reporte_enviado = None
    tz_ar = timezone(timedelta(hours=-3))
    
    while True:
        buffer_nuevas = []
        buffer_cerradas = []
        
        try:
            with requests.Session() as session:
                session.mount('https://', TLSAdapter())
                session.headers.update({'User-Agent': 'Mozilla/5.0'})

                login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
                payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
                session.post(login_url, data=payload, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)

                # LE PEDIMOS A SOLR LAS PUBLICADAS Y LAS DESIGNADAS
                url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
                params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND (estado:"Publicada" OR estado:"Designada")', "rows": "1000", "wt": "json"}
                r = session.get(url_solr, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
            
                if r.status_code == 200:
                    docs = r.json().get("response", {}).get("docs", [])
                    hallazgos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo","")).upper()]
                    ts = int(time.time() * 1000)

                    for info in hallazgos:
                        id_o = info.get('idoferta')
                        estado_actual = str(info.get('estado', '')).upper()
                        
                        estado_previo = None

                        # 1. LEER MEMORIA (Upstash o Local)
                        if UPSTASH_URL and UPSTASH_TOKEN:
                            base_url = UPSTASH_URL.rstrip('/')
                            headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
                            try:
                                resp = requests.get(f"{base_url}/get/oferta_{id_o}", headers=headers, timeout=5)
                                if resp.status_code == 200:
                                    estado_previo = resp.json().get("result")
                            except Exception as e:
                                print(f"[-] Fallo Redis GET, usando RAM local: {e}")
                                estado_previo = ofertas_estados_local.get(id_o)
                        else:
                            estado_previo = ofertas_estados_local.get(id_o)

                        # 2. MÁQUINA DE ESTADOS Y FILTROS
                        es_nueva_y_publicada = False
                        cambio_a_designada = False

                        if not estado_previo:
                            # La vemos por primera vez
                            if estado_actual == "PUBLICADA":
                                es_nueva_y_publicada = True
                            # Si la vemos por primera vez y ya está DESIGNADA, simplemente la memorizamos en silencio.
                        else:
                            # Ya la conocíamos. ¿Cambió?
                            if estado_previo == "PUBLICADA" and estado_actual == "DESIGNADA":
                                cambio_a_designada = True

                        # 3. ACTUALIZAR MEMORIA (Solo si es nueva o cambió)
                        if not estado_previo or (estado_previo != estado_actual):
                            if UPSTASH_URL and UPSTASH_TOKEN:
                                try:
                                    requests.get(f"{base_url}/set/oferta_{id_o}/{estado_actual}", headers=headers, timeout=5)
                                except:
                                    ofertas_estados_local[id_o] = estado_actual
                            else:
                                ofertas_estados_local[id_o] = estado_actual

                        # 4. PREPARAR MENSAJES
                        escuela = html.escape(str(info.get('escuela', '')))
                        cargo = html.escape(str(info.get('cargo', '')))
                        
                        if es_nueva_y_publicada:
                            ranking = obtener_top_postulantes(session, id_o)
                            link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"
                            
                            txt = f"🏫 <b>Escuela:</b> {escuela}\n"
                            txt += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                            txt += f"🏆 <b>Top 3:</b>\n{ranking}"
                            txt += f"🔗 <a href=\"{html.escape(link, quote=True)}\">VER ESCUELA</a>\n"
                            txt += "───────────────────\n"
                            buffer_nuevas.append(txt)

                        elif cambio_a_designada:
                            # No hace falta ranking ni link, ya se cerró
                            txt = f"🏫 <b>Escuela:</b> {escuela}\n"
                            txt += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                            txt += "───────────────────\n"
                            buffer_cerradas.append(txt)

                    ahora = datetime.now(tz_ar)
                    hora_actual = ahora.hour
                    hora_str = ahora.strftime("%H:%M")

                    # ENVIAR CARGOS NUEVOS
                    if buffer_nuevas:
                        bloque = ""
                        for idx, txt in enumerate(buffer_nuevas, 1):
                            bloque += txt
                            if idx % 10 == 0 or idx == len(buffer_nuevas):
                                enviar_telegram(f"🚨 <b>NUEVOS CARGOS ENCONTRADOS ({hora_str} hs)</b> 🚨\n\n{bloque}")
                                bloque = ""
                                time.sleep(2)
                                
                    # ENVIAR AVISOS DE CIERRE
                    if buffer_cerradas:
                        bloque = ""
                        for idx, txt in enumerate(buffer_cerradas, 1):
                            bloque += txt
                            if idx % 15 == 0 or idx == len(buffer_cerradas):
                                enviar_telegram(f"❌ <b>CARGOS DESIGNADOS (Ya no disponibles)</b> ❌\n\n{bloque}", silencioso=True)
                                bloque = ""
                                time.sleep(2)
                    
                    # REGISTRO DE REPORTE DE SALUD
                    if (buffer_nuevas or buffer_cerradas) and hora_actual in HORAS_REPORTE:
                        ultimo_reporte_enviado = hora_actual
                    elif not buffer_nuevas and not buffer_cerradas and hora_actual in HORAS_REPORTE and ultimo_reporte_enviado != hora_actual:
                        enviar_telegram(f"⏳ <i>{hora_str} hs - Bot activo: Monitoreando sin novedades por el momento.</i>", silencioso=True)
                        ultimo_reporte_enviado = hora_actual

                else:
                    print(f"[!] Consulta devuelta con estado {r.status_code}", flush=True)

            print(f"[*] Revisión finalizada ({datetime.now(tz_ar).strftime('%H:%M')}).", flush=True)
        except Exception as e:
            print(f"[-] Error: {e}", flush=True)
        
        time.sleep(900)

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    time.sleep(5)
    monitorear()
