import os
import ssl
import requests
import urllib3
import time
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURACIÓN ---
CUIL = os.environ.get("CUIL")
PASSWORD = os.environ.get("PASSWORD")
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Masivo 15-Batch Activo")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"[*] Servidor web escuchando en puerto {port}...", flush=True)
    server.serve_forever()

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def enviar_telegram(mensaje, silencioso=False):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": mensaje, 
        "parse_mode": "Markdown", 
        "disable_web_page_preview": True,
        "disable_notification": silencioso # Para que el latido no haga sonar el celular
    }
    try:
        requests.post(url, json=payload)
    except:
        pass

def obtener_top_postulantes(session, id_oferta):
    url_p = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "3", "wt": "json"}
    try:
        r = session.get(url_p, params=params, verify=False)
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            if not docs: return "_Sin postulantes aún_"
            res = ""
            for i, p in enumerate(docs, 1):
                nombre = f"{p.get('apellido','')} {p.get('nombre','')}".title()
                res += f"  {i}º {nombre} | *{p.get('puntaje','0.00')} pts*\n"
            return res
    except: return "_Error en ranking_"
    return "_Sin datos_"

def monitorear():
    print("[*] Monitoreo iniciado con latido de vida...", flush=True)
    ofertas_avisadas = set()
    
    while True:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        try:
            # Login ABC
            login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
            payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
            session.post(login_url, data=payload, verify=False)
            
            # Consulta
            url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
            params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Designada"', "rows": "1000", "wt": "json"}
            r = session.get(url_solr, params=params, verify=False)
            
            if r.status_code == 200:
                docs = r.json().get("response", {}).get("docs", [])
                hallazgos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo","")).upper()]
                
                bloque_mensaje = ""
                contador = 0
                ts = int(time.time() * 1000)
                hubo_novedades = False # Empezamos asumiendo que no hay nada nuevo

                for info in hallazgos:
                    id_o = info.get('idoferta')
                    if id_o not in ofertas_avisadas:
                        hubo_novedades = True # ¡Encontramos algo nuevo!
                        ranking = obtener_top_postulantes(session, id_o)
                        link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"
                        
                        bloque_mensaje += f"🏫 **Escuela:** {info.get('escuela')}\n"
                        bloque_mensaje += f"📚 **Área:** `{info.get('cargo')}`\n"
                        bloque_mensaje += f"🏆 **Top 3:**\n{ranking}"
                        bloque_mensaje += f"🔗 [POSTULARSE]({link})\n"
                        bloque_mensaje += "───────────────────\n"
                        
                        ofertas_avisadas.add(id_o)
                        contador += 1

                        if contador >= 15:
                            enviar_telegram(f"🚨 **INFORME DE CARGOS (15)** 🚨\n\n{bloque_mensaje}")
                            bloque_mensaje = ""
                            contador = 0
                            time.sleep(2)

                if bloque_mensaje:
                    enviar_telegram(f"🚨 **INFORME DE CARGOS (FINAL)** 🚨\n\n{bloque_mensaje}")

                # --- LATIDO DE VIDA ---
                if not hubo_novedades:
                    # Obtenemos la hora actual en formato HH:MM
                    hora_actual = time.strftime("%H:%M")
                    # Enviamos un mensaje silencioso para no molestar con notificaciones
                    enviar_telegram(f"⏳ _{hora_actual} hs - Búsqueda automática completada. Sin cargos nuevos._", silencioso=True)

            print("[*] Vuelta de monitoreo finalizada.", flush=True)
        except Exception as e:
            print(f"[-] Error: {e}", flush=True)
        
        time.sleep(900) # 15 minutos

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    time.sleep(5)
    
    monitorear()
