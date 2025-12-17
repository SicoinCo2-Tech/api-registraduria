import logging
import time
import random
import atexit
import uuid
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Sistema de jobs en memoria
jobs = {}
jobs_lock = threading.Lock()

# Playwright globals
playwright_instance = None
browser_instance = None

# 2Captcha API Key
TWOCAPTCHA_API_KEY = os.getenv('TWOCAPTCHA_API_KEY', '0d63c6952010e4e5d0d390db1d951ba5')

def cleanup_old_jobs():
    """Limpia jobs antiguos (>10 minutos)"""
    with jobs_lock:
        cutoff = datetime.now() - timedelta(minutes=10)
        to_delete = [
            job_id for job_id, job in jobs.items()
            if job.get('created_at', datetime.now()) < cutoff
        ]
        for job_id in to_delete:
            del jobs[job_id]
            logger.info(f"🗑️ Job {job_id} limpiado")

def solve_recaptcha(site_key, page_url):
    """Resuelve reCAPTCHA usando 2Captcha"""
    if not TWOCAPTCHA_API_KEY:
        logger.error("No se configuró TWOCAPTCHA_API_KEY")
        return None
    
    logger.info("🤖 Enviando CAPTCHA a 2Captcha...")
    
    try:
        # Enviar captcha
        response = requests.post('http://2captcha.com/in.php', data={
            'key': TWOCAPTCHA_API_KEY,
            'method': 'userrecaptcha',
            'googlekey': site_key,
            'pageurl': page_url,
            'json': 1
        }, timeout=10)
        
        result = response.json()
        if result.get('status') != 1:
            logger.error(f"Error enviando captcha: {result}")
            return None
        
        captcha_id = result.get('request')
        logger.info(f"✓ CAPTCHA enviado, ID: {captcha_id}")
        
        # Esperar resultado (máximo 2 minutos)
        for attempt in range(24):
            time.sleep(5)
            logger.info(f"Verificando CAPTCHA... intento {attempt + 1}/24")
            
            response = requests.get('http://2captcha.com/res.php', params={
                'key': TWOCAPTCHA_API_KEY,
                'action': 'get',
                'id': captcha_id,
                'json': 1
            }, timeout=10)
            
            result = response.json()
            if result.get('status') == 1:
                token = result.get('request')
                logger.info("✅ CAPTCHA resuelto!")
                return token
            elif result.get('request') == 'CAPCHA_NOT_READY':
                continue
            else:
                logger.error(f"Error obteniendo resultado: {result}")
                return None
        
        logger.error("⏱️ Timeout esperando resolución del CAPTCHA")
        return None
        
    except Exception as e:
        logger.error(f"Error en solve_recaptcha: {e}")
        return None

def init_browser():
    """Inicializa Playwright y browser"""
    global playwright_instance, browser_instance
    
    try:
        if browser_instance and browser_instance.is_connected():
            return browser_instance
        
        logger.info("🚀 Iniciando Playwright...")
        
        if playwright_instance is None:
            playwright_instance = sync_playwright().start()
        
        browser_instance = playwright_instance.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-gpu',
            ]
        )
        logger.info("✅ Browser iniciado")
        return browser_instance
        
    except Exception as e:
        logger.error(f"❌ Error iniciando browser: {e}")
        raise

def cleanup():
    """Limpia recursos"""
    global browser_instance, playwright_instance
    logger.info("🧹 Limpiando recursos...")
    if browser_instance:
        try:
            browser_instance.close()
        except:
            pass
    if playwright_instance:
        try:
            playwright_instance.stop()
        except:
            pass

atexit.register(cleanup)

def process_cedula_job(job_id, cedula_str):
    """Procesa una consulta de cédula en background"""
    context = None
    page = None
    
    try:
        # Actualizar estado
        with jobs_lock:
            jobs[job_id]['status'] = 'processing'
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"📋 Procesando job {job_id}: {cedula_str}")
        
        browser = init_browser()
        
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='es-CO',
            timezone_id='America/Bogota',
            viewport={'width': 1920, 'height': 1080},
        )
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-CO', 'es']});
            window.chrome = {runtime: {}};
        """)
        
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,ico}", lambda route: route.abort())
        
        page_url = 'https://wsp.registraduria.gov.co/censo/consultar'
        
        logger.info("🌐 Navegando a Registraduría...")
        page.goto(page_url, wait_until='networkidle', timeout=30000)
        
        # Esperar formulario
        page.wait_for_selector('input#nuip', state='visible', timeout=10000)
        time.sleep(random.uniform(1, 2))
        
        # Llenar cédula
        logger.info(f"✍️ Ingresando cédula: {cedula_str}")
        page.type('input#nuip', cedula_str, delay=random.randint(80, 150))
        
        time.sleep(random.uniform(1, 2))
        
        # Seleccionar tipo de elección
        try:
            options = page.eval_on_selector('select#tipo', 'el => Array.from(el.options).map(opt => ({value: opt.value, text: opt.text}))')
            valid_options = [opt for opt in options if opt['value'] != '-1']
            if valid_options:
                page.select_option('select#tipo', valid_options[0]['value'])
                logger.info(f"✓ Elección seleccionada: {valid_options[0]['text']}")
        except Exception as e:
            logger.warning(f"Error seleccionando elección: {e}")
        
        time.sleep(random.uniform(1, 2))
        
        # Resolver reCAPTCHA
        site_key = '6LcthjAgAAAAAFIQLxy52074zanHv47cIvmIHglH'
        
        with jobs_lock:
            jobs[job_id]['status'] = 'solving_captcha'
            jobs[job_id]['message'] = 'Resolviendo CAPTCHA con 2Captcha...'
            jobs[job_id]['updated_at'] = datetime.now()
        
        captcha_token = solve_recaptcha(site_key, page_url)
        
        if not captcha_token:
            logger.error("❌ No se pudo resolver el CAPTCHA")
            with jobs_lock:
                jobs[job_id]['status'] = 'captcha_failed'
                jobs[job_id]['result'] = {
                    "status": "error",
                    "mensaje": "No se pudo resolver el CAPTCHA. Verifica tu saldo en 2Captcha.",
                    "cedula": cedula_str
                }
                jobs[job_id]['updated_at'] = datetime.now()
            return
        
        # Inyectar token del captcha en el textarea
        logger.info("💉 Inyectando token de CAPTCHA...")
        page.evaluate(f"""
            var textarea = document.getElementById('g-recaptcha-response');
            if (textarea) {{
                textarea.innerHTML = '{captcha_token}';
                textarea.value = '{captcha_token}';
            }}
        """)
        
        time.sleep(1)
        
        # Enviar formulario
        logger.info("📤 Enviando formulario...")
        try:
            with page.expect_navigation(wait_until='networkidle', timeout=30000):
                page.click('input[type="submit"]#enviar')
        except PlaywrightTimeoutError:
            logger.warning("⚠️ Timeout en navegación, verificando contenido...")
        
        time.sleep(3)
        
        # Obtener contenido
        texto = page.inner_text('body')
        html = page.content()
        
        logger.info("📄 Contenido obtenido, parseando datos...")
        
        # Detectar errores
        texto_lower = texto.lower()
        if 'no se encontro' in texto_lower or 'no existe' in texto_lower:
            with jobs_lock:
                jobs[job_id]['status'] = 'not_found'
                jobs[job_id]['result'] = {
                    "status": "not_found",
                    "mensaje": "Cédula no encontrada en el sistema",
                    "cedula": cedula_str
                }
                jobs[job_id]['updated_at'] = datetime.now()
            return
        
        # Parsear datos de la tabla
        resultado = {
            "nuip": None,
            "departamento": None,
            "municipio": None,
            "puesto": None,
            "direccion": None,
            "mesa": None
        }
        
        try:
            # Intentar extraer de celdas de tabla
            cells = page.query_selector_all('td')
            if cells and len(cells) >= 6:
                resultado['nuip'] = cells[0].inner_text().strip()
                resultado['departamento'] = cells[1].inner_text().strip()
                resultado['municipio'] = cells[2].inner_text().strip()
                resultado['puesto'] = cells[3].inner_text().strip()
                resultado['direccion'] = cells[4].inner_text().strip()
                resultado['mesa'] = cells[5].inner_text().strip()
                logger.info(f"✅ Datos extraídos de tabla")
            else:
                # Fallback: buscar en texto
                logger.info("⚠️ Tabla no encontrada, buscando en texto...")
                lineas = texto.split('\n')
                for linea in lineas:
                    linea_clean = linea.strip()
                    if linea_clean.isdigit() and len(linea_clean) >= 8 and len(linea_clean) <= 10:
                        if not resultado['nuip']:
                            resultado['nuip'] = linea_clean
                        elif linea_clean != resultado['nuip'] and len(linea_clean) <= 3:
                            resultado['mesa'] = linea_clean
                    elif any(dept in linea_clean.upper() for dept in ['RISARALDA', 'VALLE', 'CUNDINAMARCA', 'ANTIOQUIA']):
                        resultado['departamento'] = linea_clean
                    elif any(mun in linea_clean.upper() for mun in ['PEREIRA', 'CALI', 'BOGOTA', 'MEDELLIN']):
                        resultado['municipio'] = linea_clean
                    elif 'IE ' in linea_clean.upper() or 'ESCUELA' in linea_clean.upper() or 'COLEGIO' in linea_clean.upper():
                        resultado['puesto'] = linea_clean
                    elif 'CRA ' in linea_clean.upper() or 'CALLE' in linea_clean.upper() or '#' in linea_clean:
                        if len(linea_clean) < 100:  # Evitar textos largos
                            resultado['direccion'] = linea_clean
        
        except Exception as e:
            logger.warning(f"Error parseando datos: {e}")
        
        # Asegurar que tenga al menos el NUIP
        if not resultado['nuip']:
            resultado['nuip'] = cedula_str
        
        # Guardar resultado exitoso
        with jobs_lock:
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['result'] = {
                "status": "success",
                "datos": resultado
            }
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"✅ Job {job_id} completado exitosamente")
        logger.info(f"📊 Datos: {resultado}")
        
    except Exception as e:
        logger.error(f"❌ Error en job {job_id}: {e}", exc_info=True)
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['result'] = {
                "status": "error",
                "mensaje": str(e)
            }
            jobs[job_id]['updated_at'] = datetime.now()
    
    finally:
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

@app.route('/health', methods=['GET'])
def health_check():
    cleanup_old_jobs()
    return jsonify({
        "status": "healthy",
        "jobs_activos": len(jobs),
        "captcha_configured": bool(TWOCAPTCHA_API_KEY),
        "api_key_preview": TWOCAPTCHA_API_KEY[:8] + "..." if TWOCAPTCHA_API_KEY else None,
        "timestamp": time.time()
    }), 200

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_async():
    """Crea un job asíncrono y retorna inmediatamente"""
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "mensaje": "No se envio JSON"}), 400
        
        cedula = data.get('cedula')
        if not cedula:
            return jsonify({"status": "error", "mensaje": "Falta cedula"}), 400
        
        cedula_str = str(cedula).strip()
        if not cedula_str.isdigit() or len(cedula_str) < 6 or len(cedula_str) > 10:
            return jsonify({"status": "error", "mensaje": "Cedula invalida"}), 400
        
        # Crear job
        job_id = str(uuid.uuid4())
        
        with jobs_lock:
            jobs[job_id] = {
                'cedula': cedula_str,
                'status': 'pending',
                'result': None,
                'created_at': datetime.now(),
                'updated_at': datetime.now()
            }
        
        # Iniciar procesamiento en background
        thread = threading.Thread(target=process_cedula_job, args=(job_id, cedula_str))
        thread.daemon = True
        thread.start()
        
        logger.info(f"🆕 Job {job_id} creado para {cedula_str}")
        
        # Retornar inmediatamente
        return jsonify({
            "status": "accepted",
            "job_id": job_id,
            "cedula": cedula_str,
            "mensaje": "Consulta iniciada. El proceso tomará 30-60 segundos (incluye resolución de CAPTCHA).",
            "endpoints": {
                "status": f"/job/{job_id}",
                "result": f"/job/{job_id}/result"
            }
        }), 202
        
    except Exception as e:
        logger.error(f"Error creando job: {e}")
        return jsonify({"status": "error", "mensaje": str(e)}), 500

@app.route('/job/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Obtiene el estado de un job"""
    with jobs_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({
            "status": "error",
            "mensaje": "Job no encontrado o expirado"
        }), 404
    
    response = {
        "job_id": job_id,
        "status": job['status'],
        "cedula": job['cedula'],
        "created_at": job['created_at'].isoformat(),
        "updated_at": job['updated_at'].isoformat()
    }
    
    if job.get('message'):
        response['mensaje'] = job['message']
    
    return jsonify(response), 200

@app.route('/job/<job_id>/result', methods=['GET'])
def get_job_result(job_id):
    """Obtiene el resultado de un job completado"""
    with jobs_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({
            "status": "error",
            "mensaje": "Job no encontrado o expirado"
        }), 404
    
    status = job['status']
    
    if status in ['pending', 'processing', 'solving_captcha']:
        return jsonify({
            "status": status,
            "mensaje": f"Job en proceso: {status}",
            "job_id": job_id,
            "nota": "El CAPTCHA puede tardar 30-60 segundos en resolverse"
        }), 202
    
    # Retornar resultado (completado, error, not_found, captcha_failed)
    return jsonify(job.get('result', {})), 200

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "servicio": "Registraduria API - 2Captcha",
        "version": "7.0",
        "captcha_service": "2Captcha",
        "captcha_configured": bool(TWOCAPTCHA_API_KEY),
        "endpoints": {
            "crear_consulta": "POST /consulta_cedula",
            "estado_job": "GET /job/{job_id}",
            "resultado_job": "GET /job/{job_id}/result",
            "health": "GET /health"
        },
        "ejemplo": {
            "paso_1": "POST /consulta_cedula con {cedula: '1087549965'}",
            "paso_2": "Guardar el job_id retornado",
            "paso_3": "Esperar 30-60 segundos",
            "paso_4": "GET /job/{job_id}/result para obtener datos"
        },
        "nota": "Cada consulta usa 1 crédito de 2Captcha (~$0.003 USD)"
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)
