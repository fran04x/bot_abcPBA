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
        self.wfile.write(b"Bot en modo Diagnostico Activo")

def run_web_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload)
        print(f"[*] Telegram respondió: {r.status_code}", flush=True)
    except Exception as e:
        print(f"[-] Error enviando a Telegram: {e}", flush=True)

def obtener_top_postulantes(session, id_oferta):
    url_postulantes = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "3", "wt": "json"}
    try:
        r = session.get(url_postulantes, params=params, verify=False)
        if r.status_code == 200:
            postulantes = r.json().get("response", {}).get("docs", [])
            if not postulantes: return "_Sin postulantes aún_"
            resumen = ""
            for i, p in enumerate(postulantes, 1):
                nombre = f"{p.get('apellido', '')} {p.get('nombre', '')}".title()
                resumen += f"  {i}º {nombre} | *{p.get('puntaje', '0.00')} pts*\n"
            return resumen
    except: return "_Error en ranking_"
    return "_Sin datos_"

def monitorear():
    print("[*] Iniciando monitoreo de prueba...", flush=True)
    # MENSAJE DE PRUEBA INICIAL
    enviar_telegram("🚀 **Test:** El bot arrancó y está buscando ofertas...")
    
    ofertas_avisadas = set()
    
    while True:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        try:
            # Login
            login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
            payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
            session.post(login_url, data=payload, verify=False)
            
            # Consulta
            url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
            # PROBANDO CON DESIGNADA
            params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Designada"', "rows": "1000", "wt": "json"}
            r = session.get(url_solr, params=params, verify=False)
            
            if r.status_code == 200:
                docs = r.json().get("response", {}).get("docs", [])
                print(f"[*] Total de ofertas 'Designada' encontradas: {len(docs)}", flush=True)
                
                # FILTRO RELAJADO PARA PRUEBAS: Cualquier cargo que diga Maestro de Grado
                nuevos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo","")).upper()]
                print(f"[*] Ofertas que pasaron el filtro de nombre: {len(nuevos)}", flush=True)

                cuerpo = "🚨 **RESULTADOS DE PRUEBA (DESIGNADAS)** 🚨\n\n"
                encontró_algo = False
                ts = int(time.time() * 1000)

                for info in nuevos:
                    id_o = info.get('idoferta')
                    if id_o not in ofertas_avisadas:
                        encontró_algo = True
                        ranking = obtener_top_postulantes(session, id_o)
                        link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"
                        
                        cuerpo += f"🏫 **Escuela:** {info.get('escuela')}\n"
                        cuerpo += f"📚 **Área:** `{info.get('cargo')}`\n"
                        cuerpo += f"🏆 **Top 3:**\n{ranking}\n"
                        cuerpo += f"🔗 [POSTULARSE]({link})\n"
                        cuerpo += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                        ofertas_avisadas.add(id_o)
                        # Limitamos a 5 para no explotar Telegram en el test
                        if len(ofertas_avisadas) > 5: break

                if encontró_algo:
                    enviar_telegram(cuerpo)
            else:
                print(f"[-] Error en el ABC: {r.status_code}", flush=True)
        except Exception as e:
            print(f"[-] Error crítico: {e}", flush=True)
        
        print("[*] Esperando 15 min...", flush=True)
        time.sleep(900)

if __name__ == "__main__":
    threading.Thread(target=monitorear, daemon=True).start()
    run_web_server()
