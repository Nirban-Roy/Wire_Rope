import json
import io
import time
import threading
from datetime import datetime
from threading import Lock

from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import paho.mqtt.client as mqtt
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font

# --- CONFIGURATION ---------------------------------------------------------

MQTT_BROKER   = "3.230.164.113"
MQTT_PORT     = 1883
MQTT_USER     = "IEMA@2024"
MQTT_PASSWORD = "Pass@IEMA2024"

# Header styling
HEADER_FILL    = PatternFill(fill_type="solid", fgColor="FFD966")  # light gold
HEADER_FONT    = Font(bold=True, color="000000")
ALT_ROW_FILL   = PatternFill(fill_type="solid", fgColor="F2F2F2")  # light grey

# --- IN-MEMORY STATE ------------------------------------------------------

data_lock = Lock()

# Holds the latest incoming values
sensor_data = {
    'sensors': {i: 0.0 for i in range(1, 9)},
    'imu': {'accelX': 0.0, 'accelY': 0.0, 'accelZ': 0.0,
            'gyroX': 0.0, 'gyroY': 0.0, 'gyroZ': 0.0},
    'steps': 0,
    'distance': 0.0  # ✅ added distance key
}


# This will hold our full report: first row = header, then one snapshot per second
log_rows = []

# Build header once
HEADERS = [
    'Timestamp',
    'Rope Length (m)',
    'Inspected Length (m)',
    'Anomaly Count',
    'Steps Count',
    'Distance (m)',
] + [f"Sensor {i} (Gauss)" for i in range(1, 9)] + [
    'Accel X (m/s²)',
    'Accel Y (m/s²)',
    'Accel Z (m/s²)',
    'Gyro X (rad/s)',
    'Gyro Y (rad/s)',
    'Gyro Z (rad/s)',
]

log_rows.append(HEADERS)

# --- MQTT CALLBACKS --------------------------------------------------------

def on_connect(client, userdata, flags, rc):
    print("Connected to MQTT broker, rc =", rc)
    for i in range(1, 10):
        client.subscribe(f"wire_rope_00{i}")
    client.subscribe("steps")

def on_message(client, userdata, msg):
    topic, payload = msg.topic, msg.payload.decode('utf-8')
    with data_lock:
        try:
            if topic.startswith("wire_rope_00"):
                idx = int(topic[-1])
                if 1 <= idx <= 8:
                    sensor_data['sensors'][idx] = float(payload)
                else:  # IMU JSON on topic 009
                    imu = json.loads(payload)
                    for axis in ('X','Y','Z'):
                        sensor_data['imu'][f"accel{axis}"] = imu.get(f"accel{axis}", 0.0)
                        sensor_data['imu'][f"gyro{axis}"]  = imu.get(f"gyro{axis}",  0.0)
            elif topic == "steps":
                sensor_data['steps'] = int(payload)
            elif topic == "distance":
                sensor_data['distance'] = float(payload)
        except Exception as e:
            print("Error processing MQTT message:", e)

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
mqtt_client.loop_start()

# --- BACKGROUND LOGGER -----------------------------------------------------

def snapshot_logger():
    """
    Every second, take a snapshot of sensor_data plus placeholders
    for rope/inspected/anomaly, and append to log_rows.
    """
    while True:
        with data_lock:
            row = [
                datetime.now().isoformat(),
                0.0,  # placeholder: ropeLength
                0.0,  # placeholder: inspectedLength
                0,    # placeholder: anomalyCount
                sensor_data['steps'],
            ]
            # 8 hall sensors
            row += [sensor_data['sensors'][i] for i in range(1, 9)]
            # 6 IMU axes
            row += [
                sensor_data['imu']['accelX'],
                sensor_data['imu']['accelY'],
                sensor_data['imu']['accelZ'],
                sensor_data['imu']['gyroX'],
                sensor_data['imu']['gyroY'],
                sensor_data['imu']['gyroZ'],
            ]
            log_rows.append(row)
        time.sleep(1)

threading.Thread(target=snapshot_logger, daemon=True).start()

# --- FLASK APP -------------------------------------------------------------

app = Flask(__name__, static_folder="static")
CORS(app)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('landing.html')

@app.route('/api/sensor-data')
def get_sensor_data():
    with data_lock:
        return jsonify(sensor_data)

@app.route('/api/publish', methods=['POST'])
def api_publish():
    data = request.get_json(force=True)
    topic, message = data.get('topic'), data.get('message')
    if not topic or message is None:
        return jsonify({"error":"Both 'topic' and 'message' required"}), 400
    try:
        mqtt_client.publish(topic, message)
        return jsonify({"status":"Message published"})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route('/api/download-report', methods=['GET','POST'])
def download_report():
    """
    Returns an .xlsx containing *all* rows in `log_rows`, 
    styled with a colored header and alternating row fills.
    """
    # Build workbook
    wb = Workbook()
    ws = wb.active

    # Write and style header
    for c, title in enumerate(log_rows[0], start=1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT

    # Write data rows with alternating fill
    for r, row in enumerate(log_rows[1:], start=2):
        fill = ALT_ROW_FILL if r % 2 == 0 else None
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            if fill:
                cell.fill = fill

    # Save to in-memory buffer
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    name = f"wire_rope_full_log_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return send_file(
        stream,
        as_attachment=True,
        download_name=name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_file(os.path.join("static", filename))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
