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

# --- CONFIGURACIÓN DESDE VARIABLES DE ENTORNO ---
CUIL = os.environ.get("CUIL")
PASSWORD = os.environ.get("PASSWORD")
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- SERVIDOR DE FACHADA PARA RENDER ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Direct-Link Activo")

def run_web_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

# --- ENVÍO DE ALERTA CON BOTÓN DE POSTULACIÓN ---
def enviar_telegram(texto, id_oferta, id_detalle):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    
    # Generamos el timestamp dinámico para el link
    ts = int(time.time() * 1000)
    link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_oferta}&detalle={id_detalle}&_t={ts}"
    
    # Solo un botón: Postulación Directa
    botones = [[{"text": "✅ POSTULARSE AQUÍ", "url": link}]]

    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps({"inline_keyboard": botones})
    }
    
    try:
        requests.post(url, json=payload)
    except:
        pass

# --- ADAPTADOR DE SEGURIDAD PARA ABC ---
class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

# --- LÓGICA DE MONITOREO ---
def monitorear():
    print("[*] Monitoreo iniciado. Filtrando: Maestro de Grado + JC.", flush=True)
    ofertas_avisadas = set()
    
    while True:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        try:
            # Login en ABC
            login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
            payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
            session.post(login_url, data=payload, verify=False)
            
            # Consulta de Ofertas Mar del Plata
            url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
            params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Publicada"', "rows": "1000", "wt": "json"}
            r = session.get(url_solr, params=params, verify=False)
            
            if r.status_code == 200:
                docs = r.json().get("response", {}).get("docs", [])
                for oferta in docs:
                    cargo = str(oferta.get("cargo", "")).upper()
                    jornada = str(oferta.get("jornada", "")).upper()
                    
                    # FILTRO: Solo Maestro de Grado y Jornada Completa
                    if "MAESTRO DE GRADO" in cargo and jornada == "JC":
                        id_o = oferta.get("idoferta")
                        id_d = oferta.get("iddetalle") or id_o # Usamos el id de detalle si existe
                        
                        if id_o not in ofertas_avisadas:
                            escuela = oferta.get('escuela', 'N/A')
                            direccion = oferta.get('domiciliodesempeno', 'Sin dirección')
                            
                            msj = (f"🚨 **NUEVA JORNADA COMPLETA** 🚨\n\n"
                                   f"🏫 **Escuela:** {escuela}\n"
                                   f"📍 **Dirección:** {direccion}\n"
                                   f"📅 **Toma de posesión:** {oferta.get('tomaposesion', '')[:10]}")
                            
                            # Enviamos un mensaje por cada cargo nuevo encontrado
                            enviar_telegram(msj, id_o, id_d)
                            ofertas_avisadas.add(id_o)
                            print(f"[+] Alerta enviada para ID: {id_o}", flush=True)
                            
            print("[*] Revisión de rutina finalizada.", flush=True)
        except Exception as e:
            print(f"[-] Error: {e}", flush=True)
            
        time.sleep(900) # Espera 15 minutos

if __name__ == "__main__":
    threading.Thread(target=monitorear, daemon=True).start()
    run_web_server()
