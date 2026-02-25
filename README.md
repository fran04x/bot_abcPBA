# 🏫 Bot Monitor APD - Servicios ABC (Provincia de Buenos Aires)

Un bot de Telegram en Python diseñado para automatizar la vigilancia de Actos Públicos Digitales (APD) en el portal de Servicios ABC. Específicamente configurado para buscar cargos de **Maestro de Grado** (Jornada Simple y Completa) en el distrito de **General Pueyrredón**, alertando en tiempo real sobre nuevas publicaciones y cargos cubiertos.

## ✨ Características Principales

- **Monitoreo en Tiempo Real:** Revisa el portal ABC constantemente en busca de nuevas ofertas en estado "Publicada".
- **Filtro Inteligente de Postulantes (Rayos X):** Extrae los puntajes de los competidores, descartando automáticamente a los docentes inactivos, revocados o que ya tomaron otro cargo, mostrando el *Top 3* real.
- **Máquina de Estados:** Detecta cuando una oferta pasa de "Publicada" a "Designada" y edita el mensaje original en Telegram para tachar el cargo cubierto.
- **Dashboard Interactivo:** Incluye un botón en Telegram para "Obtener Resultados Actuales". Al presionarlo, el bot limpia el historial del chat y envía una lista fresca y ordenada de los cargos disponibles en ese segundo.
- **Arquitectura de Alta Disponibilidad:** Preparado para deploys sin tiempo de inactividad (Zero-Downtime) en la nube. Utiliza candados distribuidos (Locks) vía Upstash Redis para evitar mensajes duplicados si hay dos instancias corriendo al mismo tiempo.
- **Bypass de Seguridad Legacy:** Incluye un adaptador TLS personalizado para poder interactuar con los servidores gubernamentales antiguos sin que la conexión sea rechazada.

## 🛠️ Tecnologías y Servicios Utilizados

- **Python 3.x** (`requests`, `urllib3`, `threading`, `http.server`)
- **Telegram Bot API** (para las notificaciones y la interfaz de botones)
- **Upstash Redis** (como base de datos rápida y gratuita para memoria y control de instancias)
- **Render** (Hosting gratuito del bot)
- **Cron-job.org** (Ping externo para evitar que Render hiberne la instancia)

## ⚙️ Variables de Entorno (.env)

Para que el bot funcione correctamente en cualquier entorno (como Render), debes configurar las siguientes variables de entorno:

| Variable | Descripción | Ejemplo |
|----------|-------------|---------|
| `CUIL` | CUIL del docente para loguearse en el ABC. | `27123456789` |
| `PASSWORD` | Contraseña de la cuenta ABC. | `MiClaveSegura123` |
| `TELEGRAM_TOKEN` | Token del bot otorgado por BotFather. | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `TELEGRAM_CHAT_ID` | ID del chat, grupo o canal donde el bot enviará los mensajes. | `-1001234567890` |
| `UPSTASH_REDIS_REST_URL` | URL REST de tu base de datos en Upstash. | `https://eu1-magical-redis-12345.upstash.io` |
| `UPSTASH_REDIS_REST_TOKEN` | Token REST de lectura/escritura de Upstash. | `AYZ...token...==` |
| `INSECURE_SSL` | (Opcional) Activa el bypass de seguridad para servidores del gobierno. | `true` |

## 🚀 Guía de Despliegue (Render + Upstash)

1. **Base de Datos:** Crea una cuenta gratuita en [Upstash](https://upstash.com/), crea una base de datos Redis y copia la `REST URL` y el `REST TOKEN`.
2. **Repositorio:** Sube este código a un repositorio en GitHub.
3. **Hosting:** Ve a [Render](https://render.com/), crea un nuevo **Web Service** conectado a tu repositorio.
4. **Configuración de Render:**
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - Configura las variables de entorno detalladas arriba.
5. **Anti-Hibernación:** Copia la URL pública que te da Render (ej. `https://mi-bot-abc.onrender.com`) y configúrala en [cron-job.org](https://cron-job.org/) para que le envíe una petición GET cada **10 minutos**.

## 📝 Modificación de Filtros

Si deseas cambiar el distrito o el cargo a monitorear, debes editar la consulta Solr (línea `params = {"q": ... }`) dentro de la función `monitorear()`. Por ejemplo, para cambiar el distrito de "GENERAL PUEYRREDON" a otro, simplemente modifica el string en el código.

---
*Desarrollado como una herramienta de asistencia y optimización para la postulación docente.*
