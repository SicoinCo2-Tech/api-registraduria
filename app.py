import os
import logging
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from threading import Lock

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Lock para evitar múltiples requests simultáneos
request_lock = Lock()

# Inicializar Playwright globalmente
playwright_instance = None
browser_instance = None

def get_browser():
    """Obtiene o crea la instancia del browser"""
    global playwright_instance, browser_instance
    
    if browser_instance is None or not browser_instance.is_connected():
        logger.info("🚀 Iniciando browser de Playwright...")
        if playwright_instance is None:
            playwright_instance = sync_playwright().start()
        
        browser_instance = playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-blink-features=AutomationControlled",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding"
            ]
        )
        logger.info("✅ Browser iniciado correctamente")
    return browser_instance

# Pre-calentar el browser al iniciar
try:
    logger.info("🔥 Pre-calentando browser...")
    get_browser()
    logger.info("✅ Browser pre-calentado")
except Exception as e:
    logger.error(f"❌ Error al pre-calentar browser: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de salud"""
    try:
        browser_status = "connected" if browser_instance and browser_instance.is_connected() else "disconnected"
        return jsonify({
            "status": "healthy",
            "browser": browser_status
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_api():
    # Usar lock para procesar una consulta a la vez (evita sobrecarga)
    if not request_lock.acquire(blocking=False):
        return jsonify({
            "status": "error",
            "mensaje": "Otra consulta en proceso. Intenta en unos segundos."
        }), 429
    
    context = None
    page = None
    
    try:
        # Validar request
        data = request.json
        if not data:
            return jsonify({"status": "error", "mensaje": "No se envió JSON"}), 400
            
        cedula = data.get('cedula')
        if not cedula:
            return jsonify({"status": "error", "mensaje": "Falta la cédula"}), 400
        
        # Validar que la cédula sea numérica
        cedula_str = str(cedula).strip()
        if not cedula_str.isdigit():
            return jsonify({"status": "error", "mensaje": "La cédula debe ser numérica"}), 400
        
        logger.info(f"📋 Consultando cédula: {cedula_str}")
        
        # Obtener browser
        browser = get_browser()
        
        # Crear contexto con configuración optimizada
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="es-CO",
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True
        )
        
        # Establecer timeout por defecto más corto
        context.set_default_timeout(45000)  # 45 segundos
        
        page = context.new_page()
        
        # Navegar a la página
        logger.info("🌐 Navegando a Registraduría...")
        response = page.goto(
            "https://wsp.registraduria.gov.co/censo/consultar.php",
            wait_until="domcontentloaded",  # Cambiado de networkidle a domcontentloaded (más rápido)
            timeout=30000
        )
        
        if not response or not response.ok:
            logger.warning(f"⚠️ Respuesta no OK: {response.status if response else 'Sin respuesta'}")
        
        # Esperar a que el formulario esté visible
        page.wait_for_selector('input[name="numdoc"]', state="visible", timeout=10000)
        
        # Llenar formulario
        logger.info("✍️ Llenando formulario...")
        page.fill('input[name="numdoc"]', cedula_str)
        
        # Click en submit y esperar navegación
        logger.info("📤 Enviando formulario...")
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=40000):
                page.click('input[type="submit"]')
        except PlaywrightTimeoutError:
            logger.warning("⚠️ Timeout en navegación, pero continuando...")
        
        # Esperar un poco más para asegurar que cargó
        page.wait_for_timeout(2000)
        
        # Obtener contenido
        html = page.content()
        texto = page.inner_text("body")
        
        logger.info("✅ Respuesta obtenida correctamente")
        
        # Detección de CAPTCHA o error
        texto_lower = texto.lower()
        html_lower = html.lower()
        
        if "captcha" in html_lower or "robot" in html_lower or "recaptcha" in html_lower:
            logger.warning("🤖 CAPTCHA detectado")
            return jsonify({
                "status": "captcha",
                "mensaje": "CAPTCHA detectado. La Registraduría requiere verificación manual.",
                "cedula": cedula_str
            }), 200  # Cambiado a 200 para que n8n no lo trate como error
        
        if "no se encontr" in texto_lower or "no existe" in texto_lower:
            logger.info("❌ Cédula no encontrada")
            return jsonify({
                "status": "not_found",
                "mensaje": "No se encontró información para esta cédula",
                "cedula": cedula_str
            }), 200
        
        # Respuesta exitosa
        return jsonify({
            "status": "success",
            "cedula": cedula_str,
            "resultado_bruto": texto,
            "html_preview": html[:500]  # Reducido a 500 caracteres
        }), 200
        
    except PlaywrightTimeoutError as e:
        logger.error(f"⏱️ Timeout error: {str(e)}")
        return jsonify({
            "status": "error",
            "mensaje": "Timeout al consultar la página. La Registraduría está lenta.",
            "error_type": "timeout"
        }), 504
        
    except Exception as e:
        logger.error(f"💥 Error inesperado: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "mensaje": "Error interno del servidor",
            "error_type": "server_error",
            "error_detail": str(e)
        }), 500
        
    finally:
        # Limpiar recursos
        request_lock.release()
        if page:
            try:
                page.close()
            except:
                pass
        if context:
            try:
                context.close()
            except:
                pass

@app.route('/', methods=['GET'])
def index():
    """Endpoint raíz"""
    return jsonify({
        "servicio": "Consulta Registraduría Colombia",
        "version": "1.0",
        "endpoints": {
            "health": "/health (GET)",
            "consulta": "/consulta_cedula (POST)"
        },
        "ejemplo": {
            "url": "/consulta_cedula",
            "method": "POST",
            "body": {"cedula": "123456789"}
        }
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)
