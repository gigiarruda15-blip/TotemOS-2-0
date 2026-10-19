"""
Sistema de gerenciamento de armários/fechaduras via placa UPUS-SKB25-V3.0,
com reconhecimento facial (cadastro e verificação de identidade).

Como rodar:
    pip install flask
    python app.py

Depois acesse http://localhost:5000
"""

import json
import logging
import math
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify, g, render_template

from board_driver import LockerBoard, LockerBoardError

# Pasta de dados: por padrão a pasta do app; no Linux (ISO) vem da
# variável TOTEM_DATA_DIR (/var/lib/totem, partição persistente).
DATA_DIR = os.environ.get("TOTEM_DATA_DIR", "")
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "lockers.db") if DATA_DIR else "lockers.db"
DEFAULT_SERIAL_PORT = "COM7" if os.name == "nt" else "/dev/ttyUSB0"

# ---------------------------------------------------------------------------
# LOG DETALHADO — tudo que acontece (comandos enviados à placa, erros,
# aberturas, falhas) fica registrado em app/logs/sistema.log. Isso é o
# que você usa pra me mandar detalhes de um erro específico.
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(DATA_DIR, "logs") if DATA_DIR else "logs"
LOG_FILE = os.path.join(LOG_DIR, "sistema.log")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("totem")
logger.setLevel(logging.DEBUG)

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_console_handler)
N_CHANNELS = 25

# O protocolo real da placa (UPUS-SKB25-V3.0) já está implementado em
# board_driver.py, testado byte a byte contra os exemplos do manual
# oficial do fabricante. A configuração (porta, endereço, baud rate e
# se está em modo simulado) agora é feita pela tela do totem, no botão
# de engrenagem — não precisa mais editar código pra isso.

# Distância euclidiana máxima entre dois descritores faciais para
# considerar "é a mesma pessoa". Ajuste conforme os testes reais.
FACE_MATCH_THRESHOLD = 0.50  # valor padrão; o valor em uso vem da configuração (banco)

app = Flask(__name__)

# O objeto de conexão com a placa não é mais fixo — é recriado sempre
# que a configuração muda (porta, endereço, baud rate), lida do banco
# de dados. Isso permite configurar tudo pela tela, sem editar código.
_board_instance = None
_board_config_cache = None


def get_board_config():
    db = get_db()
    row = db.execute("SELECT * FROM board_config WHERE id = 1").fetchone()
    return dict(row)


def get_board():
    """Retorna a instância de LockerBoard, recriando se a config mudou."""
    global _board_instance, _board_config_cache
    cfg = get_board_config()

    if _board_instance is None or _board_config_cache != cfg:
        if _board_instance is not None:
            try:
                _board_instance.disconnect()
            except Exception:
                pass
        _board_instance = LockerBoard(
            port=cfg["serial_port"],
            baudrate=cfg["baudrate"],
            address=cfg["board_address"],
            response_timeout=cfg["response_timeout_ms"] / 1000.0,
            idle_silence=cfg["idle_silence_ms"] / 1000.0,
        )
        _board_config_cache = cfg

    return _board_instance


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return "", 204


@app.errorhandler(Exception)
def handle_any_error(e):
    """
    Rede de segurança: qualquer erro não previsto (ex: falha de
    comunicação serial no meio da leitura da resposta, depois que a
    placa já destravou fisicamente) cai aqui. Sem isso, o Flask
    devolveria uma página HTML de erro, o navegador tentaria
    interpretar como JSON e falharia, mostrando uma mensagem genérica
    e escondendo o motivo real. Agora sempre volta um JSON com o erro
    de verdade, e tudo fica registrado no log.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        # erros HTTP normais (404, 405, etc) seguem o comportamento padrão
        return e

    tb = traceback.format_exc()
    logger.error(f"ERRO NÃO TRATADO em {request.method} {request.path}: {e}\n{tb}")
    return jsonify({
        "success": False,
        "error": f"Erro interno: {e}",
    }), 500


# ---------------------------------------------------------------------------
# Banco de dados (histórico de aberturas + estado lógico dos armários)
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lockers (
            channel INTEGER PRIMARY KEY,
            label TEXT,
            occupied INTEGER DEFAULT 0,
            user_ref TEXT,
            face_descriptor TEXT,
            photo TEXT,
            occupied_at TEXT
        )
    """)
    # Migração leve: se o banco já existia sem a coluna, adiciona agora.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lockers)").fetchall()]
    if "face_descriptor" not in cols:
        conn.execute("ALTER TABLE lockers ADD COLUMN face_descriptor TEXT")
    if "photo" not in cols:
        conn.execute("ALTER TABLE lockers ADD COLUMN photo TEXT")
    if "occupied_at" not in cols:
        conn.execute("ALTER TABLE lockers ADD COLUMN occupied_at TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel INTEGER,
            action TEXT,
            user_ref TEXT,
            timestamp TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS board_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            serial_port TEXT DEFAULT 'COM7',
            board_address INTEGER DEFAULT 1,
            baudrate INTEGER DEFAULT 9600,
            simulate INTEGER DEFAULT 1,
            admin_pin TEXT DEFAULT '1234',
            response_timeout_ms INTEGER DEFAULT 2000,
            idle_silence_ms INTEGER DEFAULT 250,
            door_open_signal TEXT DEFAULT 'disconnected',
            sensor_poll_interval_seconds INTEGER DEFAULT 5,
            sensor_notify_open INTEGER DEFAULT 0,
            sensor_notify_left_open INTEGER DEFAULT 0,
            door_close_grace_seconds INTEGER DEFAULT 20,
            sensor_notify_violation INTEGER DEFAULT 0,
            violation_window_seconds INTEGER DEFAULT 15,
            face_match_threshold REAL DEFAULT 0.5
        )
    """)
    # Migração leve para bancos já existentes sem as colunas novas
    cfg_cols = [r[1] for r in conn.execute("PRAGMA table_info(board_config)").fetchall()]
    new_cols = {
        "admin_pin": "TEXT DEFAULT '1234'",
        "response_timeout_ms": "INTEGER DEFAULT 2000",
        "idle_silence_ms": "INTEGER DEFAULT 250",
        "door_open_signal": "TEXT DEFAULT 'disconnected'",
        "sensor_poll_interval_seconds": "INTEGER DEFAULT 5",
        "sensor_notify_open": "INTEGER DEFAULT 0",
        "sensor_notify_left_open": "INTEGER DEFAULT 0",
        "door_close_grace_seconds": "INTEGER DEFAULT 20",
        "sensor_notify_violation": "INTEGER DEFAULT 0",
        "violation_window_seconds": "INTEGER DEFAULT 15",
        "face_match_threshold": "REAL DEFAULT 0.5",
    }
    for col, col_def in new_cols.items():
        if col not in cfg_cols:
            conn.execute(f"ALTER TABLE board_config ADD COLUMN {col} {col_def}")

    conn.execute(
        "INSERT OR IGNORE INTO board_config "
        "(id, serial_port, board_address, baudrate, simulate, admin_pin) "
        "VALUES (1, ?, 1, 9600, 1, '1234')",
        (DEFAULT_SERIAL_PORT,),
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS door_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel INTEGER,
            kind TEXT,
            message TEXT,
            created_at TEXT,
            resolved INTEGER DEFAULT 0,
            resolved_at TEXT
        )
    """)

    for ch in range(1, N_CHANNELS + 1):
        conn.execute(
            "INSERT OR IGNORE INTO lockers (channel, label) VALUES (?, ?)",
            (ch, f"Armário {ch}"),
        )
    conn.commit()
    conn.close()


def log_event(channel, action, user_ref=None):
    db = get_db()
    db.execute(
        "INSERT INTO events (channel, action, user_ref, timestamp) VALUES (?, ?, ?, ?)",
        (channel, action, user_ref, datetime.now().isoformat()),
    )
    db.commit()


def euclidean_distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def open_channel_physically(channel):
    """Aciona a placa de verdade, ou simula, conforme a configuração salva."""
    cfg = get_board_config()
    if cfg["simulate"]:
        logger.info(f"Canal {channel}: abertura SIMULADA (modo simulado ativo)")
        return True

    logger.info(
        f"Canal {channel}: enviando comando de destravamento real "
        f"(porta={cfg['serial_port']}, endereço={cfg['board_address']}, baud={cfg['baudrate']})"
    )
    board = get_board()
    if not board.is_connected():
        board.connect()
    result = board.open_channel(channel)
    logger.info(f"Canal {channel}: placa respondeu, destravamento = {result}")
    if result:
        _last_open_at[channel] = datetime.now()
    return result


def raise_door_alert(channel, kind, message):
    """Cria um alerta se ainda não houver um igual ativo pra esse canal."""
    db = get_db()
    existing = db.execute(
        "SELECT id FROM door_alerts WHERE channel=? AND kind=? AND resolved=0",
        (channel, kind),
    ).fetchone()
    if existing:
        return  # já existe um alerta igual ativo, não duplica

    db.execute(
        "INSERT INTO door_alerts (channel, kind, message, created_at, resolved) VALUES (?, ?, ?, ?, 0)",
        (channel, kind, message, datetime.now().isoformat()),
    )
    db.commit()
    logger.warning(f"ALERTA canal {channel} [{kind}]: {message}")


def resolve_door_alerts(channel, kind=None):
    """Marca como resolvido qualquer alerta ativo desse canal (e tipo, se informado)."""
    db = get_db()
    if kind:
        rows = db.execute(
            "SELECT id FROM door_alerts WHERE channel=? AND kind=? AND resolved=0", (channel, kind)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id FROM door_alerts WHERE channel=? AND resolved=0", (channel,)
        ).fetchall()

    for row in rows:
        db.execute(
            "UPDATE door_alerts SET resolved=1, resolved_at=? WHERE id=?",
            (datetime.now().isoformat(), row["id"]),
        )
        logger.info(f"Alerta #{row['id']} do canal {channel} resolvido")
    if rows:
        db.commit()


def has_active_alert(channel, kind):
    """Verifica se já existe um alerta ATIVO (não resolvido) desse tipo pra esse canal."""
    db = get_db()
    row = db.execute(
        "SELECT id FROM door_alerts WHERE channel=? AND kind=? AND resolved=0", (channel, kind)
    ).fetchone()
    return row is not None


def check_door_opened_by_sensor(channel):
    """
    OPÇÃO 1 — Notificação de abertura de porta.
    Depois de destravar, confirma pelo sensor de fim de curso se a
    porta realmente abriu. Só roda se sensor_notify_open estiver
    ligado e não estiver em modo simulado. Faz algumas tentativas
    rápidas (a porta pode levar um instante pra realmente abrir depois
    do destravamento elétrico). Se o sensor nunca confirmar, indica
    que a porta está com defeito.

    Retorno:
      None  -> verificação está DESLIGADA (não afirma nada)
      True  -> sensor confirmou que a porta abriu
      False -> verificação está LIGADA, mas não foi possível confirmar
               (sinal nunca mudou, ou nem deu pra ler o sensor) —
               tratado como possível defeito na porta/sensor
    """
    cfg = get_board_config()
    if cfg["simulate"] or not cfg["sensor_notify_open"]:
        return None  # verificação desligada, não afirma nada

    expected_open_value = cfg["door_open_signal"]  # 'connected' ou 'disconnected'
    board = get_board()
    read_failures = 0

    for attempt in range(4):
        try:
            signal = board.get_channel_signal(channel)
        except LockerBoardError as e:
            read_failures += 1
            logger.warning(
                f"Canal {channel}: falha ao ler sensor (tentativa {attempt + 1}/4) — {e}"
            )
            time.sleep(0.3)
            continue  # tenta de novo, NÃO desiste com sucesso silencioso

        if signal == expected_open_value:
            resolve_door_alerts(channel, "defect")
            logger.info(f"Canal {channel}: sensor CONFIRMOU que a porta abriu")
            return True

        time.sleep(0.5)

    if read_failures == 4:
        message = (
            f"Armário {channel}: possível defeito — não foi possível ler o sensor "
            f"depois do comando de destravamento (falha de comunicação)."
        )
    else:
        message = (
            f"Armário {channel}: possível defeito na porta ou no sensor — o comando "
            f"de destravamento foi enviado, mas o sensor de fim de curso não detectou "
            f"nenhuma mudança."
        )

    raise_door_alert(channel, "defect", message)
    logger.warning(f"Canal {channel}: sensor NÃO confirmou abertura após destravar ({message})")
    return False


# ---------------------------------------------------------------------------
# Rotas da API
# ---------------------------------------------------------------------------
@app.route("/api/lockers", methods=["GET"])
def list_lockers():
    db = get_db()
    rows = db.execute(
        "SELECT channel, label, occupied, user_ref, face_descriptor, occupied_at "
        "FROM lockers ORDER BY channel"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/lockers/<int:channel>/register-face", methods=["POST"])
def register_face(channel):
    """
    Cadastra o rosto de quem vai guardar um item nesse armário, e já
    manda abrir a porta física. Chamado quando o armário estava LIVRE.
    """
    logger.info(f"Canal {channel}: pedido de cadastro facial recebido")
    data = request.get_json(force=True)
    descriptor = data.get("descriptor")
    photo = data.get("photo")
    if not descriptor or not isinstance(descriptor, list):
        return jsonify({"success": False, "error": "descriptor ausente ou inválido"}), 400

    try:
        opened = open_channel_physically(channel)
    except LockerBoardError as e:
        logger.error(f"Canal {channel}: falha ao destravar (cadastro) — {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    if not opened:
        logger.warning(f"Canal {channel}: placa respondeu mas não confirmou destravamento (cadastro)")
        return jsonify({"success": False, "error": "Falha ao destravar a porta"}), 500

    # IMPORTANTE: a checagem por sensor acontece ANTES de gravar
    # qualquer coisa no banco. Se o sensor de fim de curso estiver
    # ativado e não confirmar a abertura, o cadastro NÃO é salvo —
    # o armário continua livre, pronto pra tentar de novo.
    door_confirmed = check_door_opened_by_sensor(channel)

    if door_confirmed is False:
        logger.warning(f"Canal {channel}: cadastro NÃO efetivado — sensor não confirmou abertura")
        return jsonify({
            "success": False,
            "door_sensor_confirmed": False,
            "error": "A porta não foi confirmada como aberta pelo sensor de fim de curso. "
                     "Nada foi salvo — verifique a porta e tente novamente.",
        })

    db = get_db()
    db.execute(
        "UPDATE lockers SET occupied = 1, user_ref = ?, face_descriptor = ?, "
        "photo = ?, occupied_at = ? WHERE channel = ?",
        ("rosto cadastrado", json.dumps(descriptor), photo,
         datetime.now().isoformat(), channel),
    )
    db.commit()
    log_event(channel, "register_face_and_open", "rosto cadastrado")
    logger.info(f"Canal {channel}: cadastro facial concluído com sucesso")

    return jsonify({"success": True, "door_sensor_confirmed": door_confirmed})


@app.route("/api/lockers/<int:channel>/verify-face", methods=["POST"])
def verify_face(channel):
    """
    Compara o rosto capturado agora com o rosto salvo para este armário.
    Só destrava e libera o armário se a distância for menor que o
    threshold configurado. Chamado quando o armário estava OCUPADO.
    """
    logger.info(f"Canal {channel}: pedido de verificação facial recebido")
    data = request.get_json(force=True)
    descriptor = data.get("descriptor")
    if not descriptor or not isinstance(descriptor, list):
        return jsonify({"success": False, "error": "descriptor ausente ou inválido"}), 400

    db = get_db()
    row = db.execute(
        "SELECT face_descriptor FROM lockers WHERE channel = ?", (channel,)
    ).fetchone()

    if row is None or not row["face_descriptor"]:
        return jsonify({
            "success": False,
            "match": False,
            "error": "Este armário não tem cadastro facial registrado"
        }), 400

    stored = json.loads(row["face_descriptor"])
    distance = euclidean_distance(descriptor, stored)
    threshold = float(get_board_config().get("face_match_threshold") or FACE_MATCH_THRESHOLD)
    match = distance <= threshold
    logger.info(f"Canal {channel}: distância facial = {distance:.4f} (limite = {threshold})")

    if not match:
        log_event(channel, "verify_face_denied", f"distance={distance:.3f}")
        logger.warning(f"Canal {channel}: rosto NÃO corresponde (distância {distance:.4f})")
        return jsonify({"success": False, "match": False, "distance": distance,
                         "error": "Rosto não corresponde ao cadastro deste armário"})

    try:
        opened = open_channel_physically(channel)
    except LockerBoardError as e:
        logger.error(f"Canal {channel}: falha ao destravar (verificação) — {e}")
        return jsonify({"success": False, "match": True, "error": str(e)}), 500

    if not opened:
        logger.warning(f"Canal {channel}: placa respondeu mas não confirmou destravamento (verificação)")
        return jsonify({"success": False, "match": True, "error": "Falha ao destravar a porta"}), 500

    # IMPORTANTE: só libera o armário no banco (apaga o rosto cadastrado)
    # SE o sensor confirmar a abertura (ou se a verificação por sensor
    # estiver desligada). Se não confirmar, o armário continua ocupado
    # com o mesmo rosto salvo, pronto pra tentar de novo.
    door_confirmed = check_door_opened_by_sensor(channel)

    if door_confirmed is False:
        logger.warning(f"Canal {channel}: retirada NÃO efetivada — sensor não confirmou abertura")
        return jsonify({
            "success": False,
            "match": True,
            "door_sensor_confirmed": False,
            "error": "A porta não foi confirmada como aberta pelo sensor de fim de curso. "
                     "O armário continua com seu cadastro — verifique a porta e tente novamente.",
        })

    db.execute(
        "UPDATE lockers SET occupied = 0, user_ref = NULL, face_descriptor = NULL, photo = NULL, occupied_at = NULL WHERE channel = ?",
        (channel,),
    )
    db.commit()
    log_event(channel, "verify_face_ok_and_open", f"distance={distance:.3f}")

    return jsonify({"success": True, "match": True, "distance": distance, "door_sensor_confirmed": door_confirmed})


@app.route("/api/lockers/<int:channel>/open", methods=["POST"])
def open_locker(channel):
    """Abertura manual/administrativa, sem passar por verificação facial."""
    user_ref = request.json.get("user_ref") if request.is_json else None

    try:
        opened = open_channel_physically(channel)
    except LockerBoardError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    if opened:
        door_confirmed = check_door_opened_by_sensor(channel)

        if door_confirmed is False:
            return jsonify({
                "success": False,
                "door_sensor_confirmed": False,
                "error": "A porta não foi confirmada como aberta pelo sensor de fim de curso.",
            })

        db = get_db()
        db.execute(
            "UPDATE lockers SET occupied = 0, user_ref = NULL, face_descriptor = NULL, photo = NULL, occupied_at = NULL WHERE channel = ?",
            (channel,),
        )
        db.commit()
        log_event(channel, "manual_open", user_ref)
        return jsonify({"success": opened, "door_sensor_confirmed": door_confirmed})

    return jsonify({"success": opened})


@app.route("/api/lockers/<int:channel>/assign", methods=["POST"])
def assign_locker(channel):
    """Marca um armário como ocupado por um usuário (sem destravar)."""
    user_ref = request.json.get("user_ref")
    db = get_db()
    db.execute(
        "UPDATE lockers SET occupied = 1, user_ref = ? WHERE channel = ?",
        (user_ref, channel),
    )
    db.commit()
    log_event(channel, "assign", user_ref)
    return jsonify({"success": True})


@app.route("/api/status", methods=["GET"])
def board_status():
    cfg = get_board_config()
    if cfg["simulate"]:
        return jsonify({"success": False, "error": "Placa em modo simulado"}), 400
    try:
        board = get_board()
        if not board.is_connected():
            board.connect()
        status = board.get_status()
    except LockerBoardError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "status": status})


# ---------------------------------------------------------------------------
# Configuração da placa (porta, endereço, baud rate, modo simulado)
# ---------------------------------------------------------------------------
@app.route("/api/board/config", methods=["GET"])
def get_config():
    cfg = get_board_config()
    return jsonify({"success": True, "config": cfg})


@app.route("/api/board/config", methods=["POST"])
def save_config():
    data = request.get_json(force=True)

    serial_port = str(data.get("serial_port", "COM7")).strip()
    try:
        board_address = int(data.get("board_address", 1))
        baudrate = int(data.get("baudrate", 9600))
        response_timeout_ms = int(data.get("response_timeout_ms", 2000))
        idle_silence_ms = int(data.get("idle_silence_ms", 250))
        door_close_grace_seconds = int(data.get("door_close_grace_seconds", 20))
        sensor_poll_interval_seconds = int(data.get("sensor_poll_interval_seconds", 5))
        violation_window_seconds = int(data.get("violation_window_seconds", 15))
        face_match_threshold = float(data.get("face_match_threshold", FACE_MATCH_THRESHOLD))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Campos numéricos inválidos"}), 400

    if not (1 <= board_address <= 32):
        return jsonify({"success": False, "error": "Endereço deve ser entre 1 e 32"}), 400
    if not serial_port:
        return jsonify({"success": False, "error": "Porta serial não pode ficar vazia"}), 400
    if not (0.2 <= face_match_threshold <= 1.5):
        return jsonify({"success": False, "error": "Limite facial deve ficar entre 0.20 e 1.50"}), 400
    if not (100 <= response_timeout_ms <= 10000):
        return jsonify({"success": False, "error": "Timeout de resposta deve ser entre 100 e 10000 ms"}), 400
    if not (20 <= idle_silence_ms <= 3000):
        return jsonify({"success": False, "error": "Silêncio de fim de resposta deve ser entre 20 e 3000 ms"}), 400

    simulate = 1 if data.get("simulate", True) else 0
    sensor_notify_open = 1 if data.get("sensor_notify_open", False) else 0
    sensor_notify_left_open = 1 if data.get("sensor_notify_left_open", False) else 0
    sensor_notify_violation = 1 if data.get("sensor_notify_violation", False) else 0
    door_open_signal = str(data.get("door_open_signal", "disconnected"))
    if door_open_signal not in ("connected", "disconnected"):
        door_open_signal = "disconnected"

    db = get_db()
    db.execute(
        """UPDATE board_config SET
             serial_port=?, board_address=?, baudrate=?, simulate=?,
             response_timeout_ms=?, idle_silence_ms=?, door_open_signal=?,
             sensor_poll_interval_seconds=?,
             sensor_notify_open=?,
             sensor_notify_left_open=?, door_close_grace_seconds=?,
             sensor_notify_violation=?, violation_window_seconds=?,
             face_match_threshold=?
           WHERE id=1""",
        (serial_port, board_address, baudrate, simulate,
         response_timeout_ms, idle_silence_ms, door_open_signal,
         sensor_poll_interval_seconds,
         sensor_notify_open,
         sensor_notify_left_open, door_close_grace_seconds,
         sensor_notify_violation, violation_window_seconds,
         face_match_threshold),
    )
    db.commit()

    # Força recriar a conexão na próxima chamada, já com a config nova
    global _board_instance, _board_config_cache
    if _board_instance is not None:
        try:
            _board_instance.disconnect()
        except Exception:
            pass
    _board_instance = None
    _board_config_cache = None

    return jsonify({"success": True, "config": get_board_config()})


@app.route("/api/board/ports", methods=["GET"])
def list_ports():
    """Lista as portas seriais disponíveis no computador, se possível."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return jsonify({"success": False, "error": "pyserial não instalado", "ports": []})

    ports = [{"device": p.device, "description": p.description} for p in list_ports.comports()]
    return jsonify({"success": True, "ports": ports})


@app.route("/api/board/test", methods=["POST"])
def board_test():
    """
    Diagnóstico: testa a comunicação com a placa sem destravar nada.
    Pode receber uma configuração temporária no corpo (pra testar antes
    de salvar) ou, se não vier nada, usa a configuração salva.
    """
    data = request.get_json(silent=True) or {}
    saved_cfg = get_board_config()

    serial_port = str(data.get("serial_port") or saved_cfg["serial_port"])
    try:
        board_address = int(data.get("board_address") or saved_cfg["board_address"])
        baudrate = int(data.get("baudrate") or saved_cfg["baudrate"])
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Endereço e baud rate devem ser números"}), 400

    test_board = LockerBoard(port=serial_port, baudrate=baudrate, address=board_address)
    try:
        test_board.connect()
        mcu_id = test_board.read_mcu_id()
        signal_status = test_board.get_signal_status()
        return jsonify({
            "success": True,
            "message": "Comunicação com a placa OK",
            "mcu_id": mcu_id,
            "signal_status": signal_status,
        })
    except LockerBoardError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        test_board.disconnect()


@app.route("/api/board/scan", methods=["POST"])
def board_scan():
    """
    Testa TODAS as portas seriais detectadas no computador (uma por
    uma), tentando ler o ID da placa em cada uma. Útil quando você não
    sabe em qual porta COM a placa está. Também testa alguns endereços
    (1 a 8) em cada porta, caso o endereço configurado esteja errado.
    """
    data = request.get_json(silent=True) or {}
    baudrate = int(data.get("baudrate") or 9600)
    address_range = data.get("address_range") or list(range(1, 9))

    try:
        from serial.tools import list_ports
    except ImportError:
        return jsonify({"success": False, "error": "pyserial não instalado", "results": []}), 400

    ports = [p.device for p in list_ports.comports()]
    if not ports:
        return jsonify({
            "success": False,
            "error": "Nenhuma porta serial detectada no computador. "
                     "Confira se o conversor USB-RS485 está conectado.",
            "results": [],
        })

    results = []
    found = None

    for port in ports:
        for address in address_range:
            test_board = LockerBoard(port=port, baudrate=baudrate, address=address)
            try:
                test_board.connect()
                mcu_id = test_board.read_mcu_id()
                results.append({
                    "port": port, "address": address, "success": True, "mcu_id": mcu_id
                })
                if found is None:
                    found = {"port": port, "address": address, "mcu_id": mcu_id}
            except LockerBoardError as e:
                results.append({
                    "port": port, "address": address, "success": False, "error": str(e)
                })
            finally:
                test_board.disconnect()

            if found:
                break
        if found:
            break

    return jsonify({
        "success": found is not None,
        "found": found,
        "results": results,
        "ports_tested": ports,
    })


# ---------------------------------------------------------------------------
# LOGS — para diagnóstico e envio de detalhes de erro
# ---------------------------------------------------------------------------
@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Retorna as últimas N linhas do log (padrão 200)."""
    n = request.args.get("lines", default=200, type=int)
    if not os.path.exists(LOG_FILE):
        return jsonify({"success": True, "lines": []})
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return jsonify({"success": True, "lines": all_lines[-n:]})


@app.route("/api/logs/download", methods=["GET"])
def download_logs():
    """Baixa o arquivo de log completo — use isso pra me enviar o erro."""
    from flask import send_file
    if not os.path.exists(LOG_FILE):
        return jsonify({"success": False, "error": "Nenhum log ainda"}), 404
    return send_file(LOG_FILE, as_attachment=True, download_name="sistema.log")


@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    open(LOG_FILE, "w", encoding="utf-8").close()
    logger.info("Log limpo manualmente pelo usuário")
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# MODO ADMIN — abrir qualquer armário sem passar pela verificação facial
# ---------------------------------------------------------------------------
@app.route("/api/admin/verify-pin", methods=["POST"])
def admin_verify_pin():
    data = request.get_json(force=True)
    pin = str(data.get("pin", ""))
    cfg = get_board_config()
    correct = str(cfg.get("admin_pin", "1234"))

    if pin == correct:
        logger.info("Acesso admin concedido")
        return jsonify({"success": True})

    logger.warning("Tentativa de acesso admin com PIN incorreto")
    return jsonify({"success": False, "error": "PIN incorreto"}), 401


@app.route("/api/admin/pin", methods=["POST"])
def admin_change_pin():
    """Troca o PIN do modo admin (pede o PIN atual pra confirmar)."""
    data = request.get_json(force=True)
    current_pin = str(data.get("current_pin", ""))
    new_pin = str(data.get("new_pin", "")).strip()

    cfg = get_board_config()
    if current_pin != str(cfg.get("admin_pin", "1234")):
        return jsonify({"success": False, "error": "PIN atual incorreto"}), 401

    if not new_pin or not new_pin.isdigit() or len(new_pin) < 4:
        return jsonify({"success": False, "error": "Novo PIN deve ter pelo menos 4 dígitos numéricos"}), 400

    db = get_db()
    db.execute("UPDATE board_config SET admin_pin = ? WHERE id = 1", (new_pin,))
    db.commit()
    logger.info("PIN de admin alterado")
    return jsonify({"success": True})


@app.route("/api/admin/lockers/<int:channel>/open", methods=["POST"])
def admin_open_locker(channel):
    """
    Abre um armário diretamente pelo modo admin, sem verificação
    facial — pede o PIN de novo aqui por segurança (não confia só no
    fato de a tela admin estar aberta).
    """
    data = request.get_json(force=True)
    pin = str(data.get("pin", ""))
    cfg = get_board_config()
    if pin != str(cfg.get("admin_pin", "1234")):
        return jsonify({"success": False, "error": "PIN incorreto"}), 401

    logger.info(f"Canal {channel}: abertura via MODO ADMIN")

    try:
        opened = open_channel_physically(channel)
    except LockerBoardError as e:
        logger.error(f"Canal {channel}: falha ao destravar (admin) — {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    if not opened:
        return jsonify({"success": False, "error": "Falha ao destravar a porta"}), 500

    # IMPORTANTE: mesma regra das outras rotas — só libera o armário no
    # banco se o sensor confirmar (ou se a verificação estiver desligada).
    door_confirmed = check_door_opened_by_sensor(channel)

    if door_confirmed is False:
        logger.warning(f"Canal {channel}: abertura admin NÃO efetivada — sensor não confirmou")
        return jsonify({
            "success": False,
            "door_sensor_confirmed": False,
            "error": "A porta não foi confirmada como aberta pelo sensor de fim de curso. "
                     "O estado do armário não foi alterado — verifique a porta e tente novamente.",
        })

    db = get_db()
    db.execute(
        "UPDATE lockers SET occupied = 0, user_ref = NULL, face_descriptor = NULL, photo = NULL, occupied_at = NULL WHERE channel = ?",
        (channel,),
    )
    db.commit()
    log_event(channel, "admin_open", "modo admin")

    return jsonify({"success": True, "door_sensor_confirmed": door_confirmed})


def _admin_pin_ok(data):
    cfg = get_board_config()
    return str(data.get("pin", "")) == str(cfg.get("admin_pin", "1234"))


@app.route("/api/admin/lockers/<int:channel>/unlock-only", methods=["POST"])
def admin_unlock_only(channel):
    """Destrava a porta pelo modo admin SEM mudar o status do armário."""
    if not _admin_pin_ok(request.get_json(force=True)):
        return jsonify({"success": False, "error": "PIN incorreto"}), 401
    try:
        opened = open_channel_physically(channel)
    except LockerBoardError as e:
        logger.error(f"Canal {channel}: falha ao destravar (unlock-only) — {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    if not opened:
        return jsonify({"success": False, "error": "Falha ao destravar a porta"}), 500
    door_confirmed = check_door_opened_by_sensor(channel)
    if door_confirmed is False:
        return jsonify({"success": False, "door_sensor_confirmed": False,
                        "error": "A porta não foi confirmada como aberta pelo sensor de fim de curso."})
    log_event(channel, "admin_unlock_only", "modo admin — sem alterar status")
    return jsonify({"success": True, "door_sensor_confirmed": door_confirmed})


@app.route("/api/admin/lockers/<int:channel>/mark-free", methods=["POST"])
def admin_mark_free(channel):
    """Marca o armário como LIVRE sem acionar a placa."""
    if not _admin_pin_ok(request.get_json(force=True)):
        return jsonify({"success": False, "error": "PIN incorreto"}), 401
    db = get_db()
    db.execute(
        "UPDATE lockers SET occupied = 0, user_ref = NULL, face_descriptor = NULL, "
        "photo = NULL, occupied_at = NULL WHERE channel = ?", (channel,))
    db.commit()
    log_event(channel, "admin_mark_free", "modo admin — mudança de status manual")
    return jsonify({"success": True})


@app.route("/api/admin/lockers/<int:channel>/details", methods=["POST"])
def admin_details(channel):
    """Foto e data/hora do cadastro de um armário (modo admin)."""
    if not _admin_pin_ok(request.get_json(force=True)):
        return jsonify({"success": False, "error": "PIN incorreto"}), 401
    row = get_db().execute(
        "SELECT channel, occupied, user_ref, photo, occupied_at FROM lockers WHERE channel = ?",
        (channel,)).fetchone()
    if row is None:
        return jsonify({"success": False, "error": "Armário não encontrado"}), 404
    return jsonify({"success": True, "details": dict(row)})


@app.route("/api/door-alerts", methods=["GET"])
def get_door_alerts():
    """Retorna os alertas de porta ATIVOS (não resolvidos)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM door_alerts WHERE resolved = 0 ORDER BY created_at DESC"
    ).fetchall()
    return jsonify({"success": True, "alerts": [dict(r) for r in rows]})


@app.route("/api/door-alerts/<int:alert_id>/dismiss", methods=["POST"])
def dismiss_door_alert(alert_id):
    """Descarta manualmente um alerta (ex: o operador já verificou no local)."""
    db = get_db()
    db.execute(
        "UPDATE door_alerts SET resolved=1, resolved_at=? WHERE id=?",
        (datetime.now().isoformat(), alert_id),
    )
    db.commit()
    return jsonify({"success": True})


@app.route("/api/events", methods=["GET"])
def list_events():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# A tela do totem (index_facial.html) fica em templates/, servida pelo
# próprio Flask. Como backend e frontend rodam no mesmo processo, não
# precisa mais de dois servidores/portas.
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    resp = app.make_response(render_template("index_facial.html"))
    # Impede que o navegador guarde uma versão antiga em cache — sem
    # isso, algumas atualizações do sistema podem não aparecer mesmo
    # depois de substituir os arquivos.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ---------------------------------------------------------------------------
# MONITORAMENTO CONTÍNUO — roda em segundo plano, cuidando de duas
# notificações independentes:
#
#   OPÇÃO 2 — Notificação de porta aberta: avisa quando o sensor fica
#   no estado "aberto" por mais tempo que a tolerância configurada,
#   não importa o motivo (foi aberta por comando ou não).
#
#   OPÇÃO 3 — Notificação de armário violado: avisa quando o sensor
#   detecta uma transição pra "aberto" sem que nenhum comando de
#   abertura tenha sido enviado recentemente por este sistema.
#
# As duas são independentes — cada uma só roda se estiver ligada.
# ---------------------------------------------------------------------------
_last_open_at = {}   # {channel: datetime} — atualizado sempre que UM COMANDO de abertura é enviado
_open_since = {}      # {channel: datetime|None} — desde quando o sensor está mostrando "aberto"
_last_signal = {}     # {channel: 'connected'|'disconnected'} — última leitura, pra detectar transição


def _monitor_loop():
    # Precisa de um contexto de app pra usar get_db()/get_board_config()
    # fora de uma requisição HTTP normal.
    with app.app_context():
        while True:
            cfg = get_board_config()
            notify_left_open = bool(cfg["sensor_notify_left_open"])
            notify_violation = bool(cfg["sensor_notify_violation"])

            try:
                if cfg["simulate"] or not (notify_left_open or notify_violation):
                    time.sleep(cfg["sensor_poll_interval_seconds"] or 5)
                    continue

                board = get_board()
                if not board.is_connected():
                    board.connect()

                signal_status = board.get_signal_status()
                expected_open = cfg["door_open_signal"]
                grace = cfg["door_close_grace_seconds"]
                violation_window = cfg["violation_window_seconds"]
                now = datetime.now()

                for channel, signal in signal_status.items():
                    is_open = (signal == expected_open)
                    prev_signal = _last_signal.get(channel)
                    just_transitioned_to_open = is_open and prev_signal is not None and prev_signal != signal
                    _last_signal[channel] = signal

                    # ---- rastreamento de "desde quando está aberta" (usado pela opção 2) ----
                    if is_open:
                        if _open_since.get(channel) is None:
                            _open_since[channel] = now
                    else:
                        _open_since[channel] = None

                    # ---- OPÇÃO 3: armário violado ----
                    if notify_violation:
                        if not is_open:
                            resolve_door_alerts(channel, "violation")
                        elif just_transitioned_to_open:
                            opened_at = _last_open_at.get(channel)
                            authorized_recently = (
                                opened_at is not None
                                and (now - opened_at).total_seconds() <= violation_window
                            )
                            if not authorized_recently:
                                raise_door_alert(
                                    channel, "violation",
                                    f"Armário {channel} foi aberto SEM nenhuma solicitação "
                                    f"registrada pelo sistema — possível violação."
                                )

                    # ---- OPÇÃO 2: porta aberta por muito tempo ----
                    if notify_left_open:
                        if not is_open:
                            resolve_door_alerts(channel, "left_open")
                        elif has_active_alert(channel, "violation"):
                            # Já existe uma violação ativa nesse canal — não faz
                            # sentido também avisar "ficou aberta", pois o motivo
                            # real (aberta sem autorização) já está sendo alertado
                            # com mais precisão. Evita dois avisos sonoros pro
                            # mesmo evento.
                            resolve_door_alerts(channel, "left_open")
                        else:
                            since = _open_since.get(channel)
                            if since is not None:
                                elapsed = (now - since).total_seconds()
                                if elapsed > grace:
                                    raise_door_alert(
                                        channel, "left_open",
                                        f"Armário {channel} está aberto há mais de {grace}s "
                                        f"e ainda não foi fechado."
                                    )

                db = get_db()
                db.commit()

            except Exception as e:
                logger.error(f"Erro no monitoramento de sensores: {e}\n{traceback.format_exc()}")
                time.sleep(3)
                continue

            time.sleep(cfg.get("sensor_poll_interval_seconds") or 5)


def start_monitor_thread():
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    logger.info("Thread de monitoramento de sensores iniciada")


if __name__ == "__main__":
    init_db()
    start_monitor_thread()
    print()
    print("=" * 60)
    print("  Sistema de Armários rodando!")
    print("  Abra no navegador: http://localhost:5000")
    print("=" * 60)
    print()
    app.run(host=os.environ.get("TOTEM_HOST", "0.0.0.0"), port=5000, debug=False)
