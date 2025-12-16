import logging
import time
import random
import atexit
import uuid
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            locale='es-CO',
            timezone_id='America/Bogota',
            viewport={'width': 1920, 'height': 1080},
        )
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            window.chrome = {runtime: {}};
        """)
        
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2,ttf,ico}", lambda route: route.abort())
        
        page.goto(
            'https://wsp.registraduria.gov.co/censo/consultar',
            wait_until='domcontentloaded',
            timeout=25000
        )
        
        page.wait_for_selector('input[name="numdoc"]', state='visible', timeout=8000)
        time.sleep(random.uniform(0.5, 1))
        
        page.type('input[name="numdoc"]', cedula_str, delay=random.randint(50, 100))
        time.sleep(random.uniform(0.5, 1))
        
        try:
            with page.expect_navigation(wait_until='domcontentloaded', timeout=30000):
                page.click('input[type="submit"]')
        except PlaywrightTimeoutError:
            logger.warning("⚠️ Timeout en navegacion")
        
        time.sleep(1.5)
        
        html = page.content()
        texto = page.inner_text('body')
        
        texto_lower = texto.lower()
        html_lower = html.lower()
        
        # Detectar CAPTCHA
        if any(word in html_lower for word in ['captcha', 'recaptcha', 'robot']):
            with jobs_lock:
                jobs[job_id]['status'] = 'captcha'
                jobs[job_id]['result'] = {
                    "status": "captcha",
                    "mensaje": "CAPTCHA detectado",
                    "cedula": cedula_str
                }
                jobs[job_id]['updated_at'] = datetime.now()
            return
        
        # Detectar no encontrado
        if any(phrase in texto_lower for phrase in ['no se encontro', 'no existe']):
            with jobs_lock:
                jobs[job_id]['status'] = 'not_found'
                jobs[job_id]['result'] = {
                    "status": "not_found",
                    "mensaje": "Cedula no encontrada",
                    "cedula": cedula_str
                }
                jobs[job_id]['updated_at'] = datetime.now()
            return
        
        # Parsear datos
        resultado = {
            "nombre": None,
            "cedula": cedula_str,
            "puesto_votacion": None,
            "direccion": None,
            "municipio": None,
            "departamento": None,
            "mesa": None
        }
        
        lineas = texto.split('\n')
        for i, linea in enumerate(lineas):
            linea_lower = linea.lower().strip()
            if 'nombre' in linea_lower and i + 1 < len(lineas):
                resultado['nombre'] = lineas[i + 1].strip()
            elif 'puesto' in linea_lower and i + 1 < len(lineas):
                resultado['puesto_votacion'] = lineas[i + 1].strip()
            elif 'direccion' in linea_lower and i + 1 < len(lineas):
                resultado['direccion'] = lineas[i + 1].strip()
            elif 'municipio' in linea_lower and i + 1 < len(lineas):
                resultado['municipio'] = lineas[i + 1].strip()
            elif 'departamento' in linea_lower and i + 1 < len(lineas):
                resultado['departamento'] = lineas[i + 1].strip()
            elif 'mesa' in linea_lower and i + 1 < len(lineas):
                resultado['mesa'] = lineas[i + 1].strip()
        
        # Guardar resultado exitoso
        with jobs_lock:
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['result'] = {
                "status": "success",
                "cedula": cedula_str,
                "datos": resultado,
                "texto_completo": texto
            }
            jobs[job_id]['updated_at'] = datetime.now()
        
        logger.info(f"✅ Job {job_id} completado")
        
    except Exception as e:
        logger.error(f"❌ Error en job {job_id}: {e}")
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
            "mensaje": "Consulta iniciada. Use GET /job/{job_id} para ver el estado",
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
    
    return jsonify({
        "job_id": job_id,
        "status": job['status'],
        "cedula": job['cedula'],
        "created_at": job['created_at'].isoformat(),
        "updated_at": job['updated_at'].isoformat()
    }), 200

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
    
    if job['status'] == 'pending' or job['status'] == 'processing':
        return jsonify({
            "status": "processing",
            "mensaje": "Job aun en proceso. Intente nuevamente en unos segundos.",
            "job_id": job_id
        }), 202
    
    # Retornar resultado (completado, error, captcha, not_found)
    return jsonify(job['result']), 200

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "servicio": "Registraduria API - Async",
        "version": "5.0",
        "modo": "asíncrono",
        "endpoints": {
            "crear_consulta": "POST /consulta_cedula",
            "estado_job": "GET /job/{job_id}",
            "resultado_job": "GET /job/{job_id}/result",
            "health": "GET /health"
        },
        "ejemplo": {
            "paso_1": "POST /consulta_cedula con {cedula: '12345678'}",
            "paso_2": "Guardar el job_id retornado",
            "paso_3": "GET /job/{job_id}/result para obtener resultado"
        }
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)
