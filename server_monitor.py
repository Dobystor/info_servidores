"""
Monitor de versiones Docker - SmartFlow Servers
Ejecutar: pip install paramiko requests pyyaml python-dotenv
Luego:    python server_monitor.py
Abrir:    http://localhost:9090
"""

import http.server
import socketserver
import socket
import threading
import time
import paramiko
import html
import requests
import yaml
import urllib3
import logging
import traceback
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/lasec/server_monitor.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("monitor")

# Desactivar warnings de SSL (certificados autofirmados)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== CONFIGURACION DE SERVIDORES =====
SERVERS = [
    {
        "name": "haulage-01.smartflow.com.mx",
        "host": os.getenv("HAULAGE01_HOST", "10.174.109.39"),
        "dns": "haulage-01.smartflow.com.mx",
        "port": 22,
        "user": os.getenv("HAULAGE01_USER", ""),
        "password": os.getenv("HAULAGE01_PASSWORD", ""),
        "ssh_key": None,
        "group": "HOD",
    },
    {
        "name": "dispatch-01-sf.smartflow.com.mx",
        "host": os.getenv("DISPATCH01_HOST", "10.174.109.16"),
        "dns": "dispatch-01-sf.smartflow.com.mx",
        "port": 22,
        "user": os.getenv("DISPATCH01_USER", ""),
        "password": os.getenv("DISPATCH01_PASSWORD", ""),
        "ssh_key": None,
        "group": "HOD",
    },
    {
        "name": "dispatch-02-sf.smartflow.com.mx",
        "host": os.getenv("DISPATCH02_HOST", "10.174.109.36"),
        "dns": "dispatch-02-sf.smartflow.com.mx",
        "port": 22,
        "user": os.getenv("DISPATCH02_USER", ""),
        "password": os.getenv("DISPATCH02_PASSWORD", ""),
        "ssh_key": None,
        "group": "SIM",
    },
    {
        "name": "sim.smartflow.com.mx",
        "host": os.getenv("SIM_HOST", "10.174.109.2"),
        "dns": "sim.smartflow.com.mx",
        "port": 22,
        "user": os.getenv("SIM_USER", ""),
        "password": os.getenv("SIM_PASSWORD", ""),
        "ssh_key": None,
        "group": "SIM",
    },
]

CMD_COMPOSE = "cat /opt/smartflow/docker-compose.yml"
REFRESH_INTERVAL = 60
PORT = 9090

# ===== ESTADO GLOBAL =====
server_data = {}
last_update = "Nunca"
lock = threading.Lock()


def ssh_exec(server, command):
    """Conecta por SSH a un servidor y ejecuta un comando."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": server["host"],
        "port": server.get("port", 22),
        "username": server["user"],
        "timeout": 10,
    }
    if server.get("ssh_key"):
        connect_kwargs["key_filename"] = server["ssh_key"]
    else:
        connect_kwargs["password"] = server["password"]
    client.connect(**connect_kwargs)
    stdin, stdout, stderr = client.exec_command(command, timeout=30)
    output = stdout.read().decode("utf-8", errors="replace")
    err_output = stderr.read().decode("utf-8", errors="replace")
    client.close()
    if err_output:
        log.warning(f"SSH stderr from {server['host']}: {err_output[:200]}")
    return output


def parse_compose(yaml_content):
    """Parsea el docker-compose.yml y retorna lista de {service, image}."""
    try:
        compose = yaml.safe_load(yaml_content)
        if not compose or not isinstance(compose, dict):
            return []
        services = []
        svc_section = compose.get("services", {})
        if not svc_section or not isinstance(svc_section, dict):
            return []
        for svc_name, svc_config in svc_section.items():
            if isinstance(svc_config, dict) and "image" in svc_config:
                services.append({
                    "service": str(svc_name),
                    "image": str(svc_config["image"]).strip('"').strip("'"),
                })
        return services
    except Exception as e:
        log.error(f"Error parseando YAML: {e}")
        return []


def fetch_license(server):
    """Obtiene info de licencia via API HTTP."""
    try:
        url = f"https://{server['dns']}/service/license/api/v3/license"
        resp = requests.get(url, timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "status": "ok",
                "organization": data.get("organization", ""),
                "nombre_cliente": data.get("nombre_cliente", ""),
                "comments": data.get("comments", ""),
                "dateCreated": data.get("dateCreated", ""),
                "services": data.get("services", []),
                "variables": data.get("variables", []),
            }
        else:
            return {"status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        log.warning(f"Error licencia {server['dns']}: {e}")
        return {"status": "error", "error": str(e)}


def fetch_server_info(server):
    """Obtiene imagenes Docker y licencia."""
    result = {"status": "ok", "services": [], "license": {}}
    try:
        output = ssh_exec(server, CMD_COMPOSE)
        result["services"] = parse_compose(output)
        log.info(f"SSH OK {server['host']}: {len(result['services'])} servicios")
    except Exception as e:
        log.error(f"SSH error {server['host']}: {e}")
        result["status"] = "error"
        result["error"] = str(e)
    try:
        result["license"] = fetch_license(server)
    except Exception as e:
        log.error(f"License error {server['host']}: {e}")
        result["license"] = {"status": "error", "error": str(e)}
    return result


def do_update():
    """Ejecuta una actualizacion de todos los servidores."""
    global server_data, last_update
    log.info("Iniciando ciclo de actualizacion...")
    results = {}
    threads = []

    def make_worker(srv):
        def worker():
            results[srv["host"]] = fetch_server_info(srv)
        return worker

    for srv in SERVERS:
        t = threading.Thread(target=make_worker(srv))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=45)

    with lock:
        server_data = results
        tz_mx = timezone(timedelta(hours=-6))
        last_update = datetime.now(tz_mx).strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"Ciclo completado. {len(results)} servidores actualizados.")


def update_loop():
    """Hilo que actualiza los datos periodicamente."""
    while True:
        try:
            do_update()
        except Exception as e:
            log.error(f"Error en update_loop: {e}\n{traceback.format_exc()}")
        time.sleep(REFRESH_INTERVAL)


def force_refresh():
    """Fuerza una actualizacion inmediata en un thread aparte."""
    t = threading.Thread(target=do_update, daemon=True)
    t.start()


def render_license_section(license_info, server_id):
    """Genera HTML para la seccion de licencia."""
    if not license_info or license_info.get("status") != "ok":
        error = license_info.get("error", "No disponible") if license_info else "No disponible"
        return f'<div class="license-info error-text">Licencia: {html.escape(str(error))}</div>'

    org = html.escape(str(license_info.get("organization", "")))
    cliente = html.escape(str(license_info.get("nombre_cliente", "")))
    comments = html.escape(str(license_info.get("comments", "")))

    services = license_info.get("services", [])
    services_html = ""
    copy_lines = []
    # Calcular ancho maximo para alinear
    max_name_len = max((len(str(s.get("display_name", s.get("identifier", "?")))) for s in services), default=10)
    for svc in services:
        name = str(svc.get("display_name", svc.get("identifier", "?")))
        exp = str(svc.get("expirationDate", "?"))
        copy_lines.append(f"  {name.ljust(max_name_len)}   {exp}")
        exp_class = ""
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d")
            now = datetime.now()
            days_left = (exp_date - now).days
            if days_left < 0:
                exp_class = "expired"
            elif days_left < 30:
                exp_class = "expiring-soon"
            elif days_left < 90:
                exp_class = "expiring-warning"
        except Exception:
            pass
        services_html += (
            f'<div class="service-row">'
            f'<span class="svc-name">{html.escape(name)}</span>'
            f'<span class="svc-exp {exp_class}">{html.escape(exp)}</span>'
            f'</div>'
        )

    if not services_html:
        services_html = '<div class="service-row dim">Sin servicios</div>'

    # Formato de copiado con encabezado
    copy_header = f"{'Servicio'.ljust(max_name_len)}   Vigencia"
    copy_separator = f"{'─' * max_name_len}   {'─' * 10}"
    copy_full = f"  {copy_header}\\n  {copy_separator}\\n" + "\\n".join(copy_lines)
    copy_text = copy_full.replace("'", "\\'").replace('"', "&quot;")

    return f"""
    <div class="license-section">
        <div class="license-header-row">
            <span class="org">{org}</span>
            <span class="cliente">{cliente}</span>
            <span class="comments">{comments}</span>
            <button class="copy-btn" onclick="copyText('{copy_text}')" title="Copiar servicios">&#128203;</button>
        </div>
        <div class="services-list">
            <div class="services-list-header">
                <span class="svc-col-name">Servicio</span>
                <span class="svc-col-exp">Vigencia</span>
            </div>
            {services_html}
        </div>
    </div>
    """


def generate_html():
    """Genera la pagina HTML con el estado actual."""
    try:
        with lock:
            data = dict(server_data)
            updated = last_update
    except Exception:
        data = {}
        updated = "Error"

    rows = ""
    for idx, srv in enumerate(SERVERS):
        info = data.get(srv["host"], {"status": "loading", "services": [], "license": {}})
        server_id = f"srv_{idx}"

        if info["status"] == "ok":
            status_badge = '<span class="badge ok">&#9679; Conectado</span>'
            docker_services = info.get("services", [])

            if docker_services:
                table_rows = ""
                copy_lines = []
                # Calcular ancho maximo de nombre de servicio
                max_svc_len = max((len(ds["service"]) for ds in docker_services), default=10)
                for ds in docker_services:
                    svc = html.escape(str(ds["service"]))
                    img = html.escape(str(ds["image"]))
                    copy_lines.append(f"  {ds['service'].ljust(max_svc_len)}   {ds['image']}")
                    table_rows += (
                        f'<tr class="docker-row" data-search="{svc.lower()} {img.lower()}">'
                        f'<td class="col-svc">{svc}</td>'
                        f'<td class="col-img">{img}</td></tr>'
                    )
                # Formato con encabezado alineado
                copy_header = f"  {'Servicio'.ljust(max_svc_len)}   Imagen"
                copy_separator = f"  {'─' * max_svc_len}   {'─' * 40}"
                copy_full = f"{html.escape(srv['name'])}\\n{copy_header}\\n{copy_separator}\\n" + "\\n".join(copy_lines)
                copy_docker = copy_full.replace("'", "\\'").replace('"', "&quot;")
                img_section = f"""
                <details class="docker-details">
                    <summary class="docker-summary">
                        <span class="arrow">&#9662;</span>
                        Docker Services ({len(docker_services)})
                        <button class="copy-btn small" onclick="event.stopPropagation();copyText('{copy_docker}')" title="Copiar todo">&#128203;</button>
                    </summary>
                    <div class="docker-content">
                        <input type="text" class="search-input" placeholder="Buscar servicio o imagen..." oninput="filterRows(this, '{server_id}')">
                        <table class="docker-table" id="{server_id}">
                            <thead><tr><th>Servicio</th><th>Imagen</th></tr></thead>
                            <tbody>{table_rows}</tbody>
                        </table>
                    </div>
                </details>
                """
            else:
                img_section = '<div class="docker-details"><div class="dim">Sin servicios Docker</div></div>'

        elif info["status"] == "error":
            status_badge = '<span class="badge error">&#9679; Error SSH</span>'
            img_section = f'<div class="docker-details error-text">{html.escape(str(info.get("error", "")))}</div>'
        else:
            status_badge = '<span class="badge loading">&#9679; Cargando...</span>'
            img_section = '<div class="docker-details dim">Esperando conexion...</div>'

        license_html = render_license_section(info.get("license", {}), server_id)

        rows += f"""
        <div class="server-card">
            <div class="server-header">
                <div class="server-title">
                    <h2>{html.escape(srv['name'])}</h2>
                    <div class="server-meta">
                        <span class="ip">{html.escape(srv['host'])}</span>
                        <span class="group-tag">{html.escape(srv['group'])}</span>
                    </div>
                </div>
                <div class="header-right">
                    {status_badge}
                </div>
            </div>
            {license_html}
            {img_section}
        </div>
        """

    return build_full_html(rows, updated)


def build_full_html(rows, updated):
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="{REFRESH_INTERVAL}">
    <title>SmartFlow - Monitor de Servidores</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Titillium+Web:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Titillium Web', sans-serif;
            background: #0d1117;
            color: #e0e0e0;
            padding: 24px;
            min-height: 100vh;
        }}
        .header {{
            text-align: center;
            margin-bottom: 32px;
            padding: 24px;
            background: linear-gradient(135deg, #161b22 0%, #1c2433 100%);
            border-radius: 12px;
            border: 1px solid #30363d;
        }}
        .header h1 {{
            color: #58a6ff;
            font-size: 26px;
            font-weight: 700;
            margin-bottom: 8px;
            letter-spacing: 0.5px;
        }}
        .header .update-info {{
            color: #8b949e;
            font-size: 13px;
            font-weight: 300;
            margin-bottom: 12px;
        }}
        .header .update-info strong {{
            color: #c9d1d9;
            font-weight: 600;
        }}
        .refresh-btn {{
            display: inline-block;
            background: #1f6feb;
            color: #ffffff;
            padding: 8px 20px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            font-family: 'Titillium Web', sans-serif;
            text-decoration: none;
            transition: background 0.2s, transform 0.1s;
            border: 1px solid #388bfd;
        }}
        .refresh-btn:hover {{
            background: #388bfd;
            transform: translateY(-1px);
        }}
        .refresh-btn:active {{
            background: #58a6ff;
            transform: translateY(0);
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(540px, 1fr));
            gap: 20px;
        }}
        .server-card {{
            background: #161b22;
            border-radius: 12px;
            padding: 22px;
            border: 1px solid #30363d;
            transition: border-color 0.3s, box-shadow 0.3s;
        }}
        .server-card:hover {{
            border-color: #58a6ff;
            box-shadow: 0 0 20px rgba(88,166,255,0.08);
        }}
        .server-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }}
        .server-title h2 {{
            color: #58a6ff;
            font-size: 17px;
            font-weight: 600;
            margin-bottom: 6px;
        }}
        .server-meta {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .ip {{
            color: #8b949e;
            font-size: 13px;
            font-weight: 300;
        }}
        .group-tag {{
            background: #1f6feb22;
            color: #58a6ff;
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.5px;
            border: 1px solid #1f6feb44;
        }}
        .header-right {{ text-align: right; }}
        .badge {{
            font-size: 12px;
            padding: 4px 12px;
            border-radius: 20px;
            font-weight: 600;
        }}
        .badge.ok {{ color: #3fb950; background: #23863522; border: 1px solid #23863544; }}
        .badge.error {{ color: #f85149; background: #da363422; border: 1px solid #da363444; }}
        .badge.loading {{ color: #d29922; background: #bb800922; border: 1px solid #bb800944; }}

        /* Licencia */
        .license-section {{
            margin-bottom: 16px;
            padding: 14px;
            background: #0d1117;
            border-radius: 8px;
            border: 1px solid #21262d;
        }}
        .license-header-row {{
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }}
        .license-header-row .org {{
            color: #d2a8ff;
            font-size: 13px;
            font-weight: 600;
        }}
        .license-header-row .cliente {{
            color: #8b949e;
            font-size: 12px;
        }}
        .license-header-row .comments {{
            color: #6e7681;
            font-size: 11px;
            font-weight: 300;
        }}
        .services-list-header {{
            display: flex;
            justify-content: space-between;
            padding: 4px 8px;
            margin-bottom: 4px;
            border-bottom: 1px solid #21262d;
        }}
        .svc-col-name {{
            font-size: 11px;
            color: #6e7681;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .svc-col-exp {{
            font-size: 11px;
            color: #6e7681;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .service-row {{
            display: flex;
            justify-content: space-between;
            font-size: 13px;
            padding: 5px 8px;
            border-radius: 4px;
            margin-bottom: 1px;
        }}
        .service-row:nth-child(odd) {{ background: #161b2266; }}
        .service-row:hover {{ background: #1f6feb11; }}
        .svc-name {{ color: #e0e0e0; font-weight: 400; }}
        .svc-exp {{ color: #3fb950; font-family: 'Titillium Web', monospace; font-weight: 600; }}
        .svc-exp.expired {{ color: #f85149; }}
        .svc-exp.expiring-soon {{ color: #f0883e; }}
        .svc-exp.expiring-warning {{ color: #d29922; }}
        .license-info {{
            font-size: 12px;
            margin-bottom: 12px;
            padding: 10px;
            background: #0d1117;
            border-radius: 8px;
            border: 1px solid #21262d;
        }}

        /* Docker details (collapsible) */
        .docker-details {{
            margin-top: 12px;
        }}
        .docker-summary {{
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            color: #8b949e;
            padding: 10px 14px;
            background: #0d1117;
            border-radius: 8px;
            border: 1px solid #21262d;
            list-style: none;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: background 0.2s, border-color 0.2s;
            user-select: none;
        }}
        .docker-summary::-webkit-details-marker {{ display: none; }}
        .docker-summary:hover {{
            background: #161b22;
            border-color: #58a6ff44;
        }}
        .docker-details[open] .docker-summary {{
            border-radius: 8px 8px 0 0;
            border-bottom: none;
            background: #161b22;
        }}
        .docker-details[open] .docker-summary .arrow {{
            transform: rotate(180deg);
        }}
        .arrow {{
            display: inline-block;
            transition: transform 0.2s;
            font-size: 14px;
        }}
        .docker-content {{
            padding: 12px 14px;
            background: #0d1117;
            border: 1px solid #21262d;
            border-top: none;
            border-radius: 0 0 8px 8px;
        }}
        .search-input {{
            width: 100%;
            background: #161b22;
            border: 1px solid #30363d;
            color: #e0e0e0;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-family: 'Titillium Web', sans-serif;
            outline: none;
            margin-bottom: 10px;
            transition: border-color 0.2s;
        }}
        .search-input:focus {{
            border-color: #58a6ff;
        }}
        .search-input::placeholder {{
            color: #6e7681;
        }}
        .copy-btn {{
            background: #21262d;
            border: 1px solid #30363d;
            color: #8b949e;
            padding: 4px 8px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            transition: all 0.2s;
        }}
        .copy-btn:hover {{
            background: #30363d;
            color: #58a6ff;
            border-color: #58a6ff44;
        }}
        .copy-btn:active {{
            background: #58a6ff;
            color: #0d1117;
        }}
        .copy-btn.small {{
            font-size: 12px;
            padding: 2px 6px;
            margin-left: auto;
        }}
        .docker-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }}
        .docker-table thead th {{
            text-align: left;
            color: #6e7681;
            padding: 6px 8px;
            border-bottom: 1px solid #21262d;
            font-weight: 600;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .docker-table tbody tr {{
            transition: background 0.15s;
        }}
        .docker-table tbody tr:nth-child(odd) {{
            background: #161b2244;
        }}
        .docker-table tbody tr:hover {{
            background: #1f6feb11;
        }}
        .docker-table tbody tr.hidden {{
            display: none;
        }}
        .col-svc {{
            padding: 5px 8px;
            color: #79c0ff;
            white-space: nowrap;
            font-weight: 400;
        }}
        .col-img {{
            padding: 5px 8px;
            font-family: 'Titillium Web', monospace;
            word-break: break-all;
            color: #c9d1d9;
            font-weight: 300;
        }}
        .dim {{ color: #6e7681; }}
        .error-text {{ color: #f85149; }}
        /* Toast */
        .toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: #58a6ff;
            color: #0d1117;
            padding: 12px 24px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            opacity: 0;
            transform: translateY(10px);
            transition: opacity 0.3s, transform 0.3s;
            pointer-events: none;
            z-index: 9999;
        }}
        .toast.show {{
            opacity: 1;
            transform: translateY(0);
        }}

        @media (max-width: 600px) {{
            .grid {{ grid-template-columns: 1fr; }}
            .server-header {{ flex-direction: column; }}
            body {{ padding: 12px; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>&#128421; SmartFlow &mdash; Monitor de Servidores</h1>
        <div class="update-info">
            Ultima actualizacion: <strong>{html.escape(updated)}</strong> &nbsp;|&nbsp;
            Auto-refresh cada {REFRESH_INTERVAL}s &nbsp;|&nbsp;
            PDYM &rarr; HOD / SIM
        </div>
        <a href="/refresh" class="refresh-btn" title="Forzar actualizacion ahora">&#8635; Actualizar ahora</a>
    </div>
    <div class="grid">
        {rows}
    </div>
    <div class="toast" id="toast">Copiado al portapapeles</div>

    <script>
        function copyText(text) {{
            var decoded = text.replace(/\\\\n/g, '\\n');
            navigator.clipboard.writeText(decoded).then(function() {{
                var toast = document.getElementById('toast');
                toast.classList.add('show');
                setTimeout(function() {{ toast.classList.remove('show'); }}, 1500);
            }});
        }}
        function filterRows(input, tableId) {{
            var filter = input.value.toLowerCase();
            var table = document.getElementById(tableId);
            if (!table) return;
            var rows = table.querySelectorAll('tbody tr.docker-row');
            rows.forEach(function(row) {{
                var text = row.getAttribute('data-search') || '';
                if (text.indexOf(filter) !== -1) {{
                    row.classList.remove('hidden');
                }} else {{
                    row.classList.add('hidden');
                }}
            }});
        }}
    </script>
</body>
</html>"""


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    """TCPServer que fuerza SO_REUSEADDR y SO_REUSEPORT."""
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == "/refresh":
                force_refresh()
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Connection", "close")
                self.end_headers()
                return

            content = generate_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            log.error(f"Error sirviendo request: {e}")
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    log.info("=== SmartFlow Server Monitor ===")
    log.info(f"Servidor HTTP en http://0.0.0.0:{PORT}")
    log.info(f"Refresh cada {REFRESH_INTERVAL} segundos")

    updater = threading.Thread(target=update_loop, daemon=True)
    updater.start()

    httpd = ReusableTCPServer(("0.0.0.0", PORT), Handler)
    try:
        log.info("Servidor HTTP iniciado.")
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Detenido por usuario.")
    except Exception as e:
        log.error(f"Error fatal: {e}\n{traceback.format_exc()}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        log.info("Servidor cerrado.")
