#include "config_server.h"

// Helper to get MAC address as clean string (lowercase, no separators)
static String getMACClean() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char macStr[13];
    snprintf(macStr, sizeof(macStr), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return String(macStr);
}

ConfigServer::ConfigServer(ConfigManager& configManager)
    : config_(configManager), server_(80), running_(false), apMode_(false) {
}

void ConfigServer::startAPMode() {
    Serial.println("ConfigServer: Starting AP mode...");

    // Stop any existing WiFi connection
    WiFi.disconnect(true);
    delay(100);

    // Configure AP
    WiFi.mode(WIFI_AP);
    WiFi.softAPConfig(CONFIG_AP_IP, CONFIG_AP_IP, IPAddress(255, 255, 255, 0));

    if (strlen(CONFIG_AP_PASSWORD) > 0) {
        WiFi.softAP(CONFIG_AP_SSID, CONFIG_AP_PASSWORD);
    } else {
        WiFi.softAP(CONFIG_AP_SSID);
    }

    delay(100);

    // Start DNS server for captive portal
    dnsServer_.start(DNS_PORT, "*", CONFIG_AP_IP);

    // Setup web server routes
    setupRoutes();
    server_.begin();

    running_ = true;
    apMode_ = true;

    Serial.printf("ConfigServer: AP started\n");
    Serial.printf("  SSID: %s\n", CONFIG_AP_SSID);
    Serial.printf("  IP: %s\n", WiFi.softAPIP().toString().c_str());
    Serial.println("  Connect to this network and open http://192.168.4.1");
}

void ConfigServer::startSTAMode() {
    Serial.println("ConfigServer: Starting STA mode...");

    setupRoutes();
    server_.begin();

    running_ = true;
    apMode_ = false;

    Serial.printf("ConfigServer: Web server started on %s\n", WiFi.localIP().toString().c_str());
}

void ConfigServer::stop() {
    if (running_) {
        server_.stop();
        if (apMode_) {
            dnsServer_.stop();
            WiFi.softAPdisconnect(true);
        }
        running_ = false;
        Serial.println("ConfigServer: Stopped");
    }
}

void ConfigServer::handleClient() {
    if (!running_) return;

    if (apMode_) {
        dnsServer_.processNextRequest();
    }
    server_.handleClient();
}

String ConfigServer::getIP() {
    if (apMode_) {
        return WiFi.softAPIP().toString();
    }
    return WiFi.localIP().toString();
}

void ConfigServer::setupRoutes() {
    server_.on("/", HTTP_GET, [this]() { handleRoot(); });
    server_.on("/save", HTTP_POST, [this]() { handleSave(); });
    server_.on("/status", HTTP_GET, [this]() { handleStatus(); });
    server_.on("/reset", HTTP_POST, [this]() { handleReset(); });
    server_.on("/reboot", HTTP_POST, [this]() { handleReboot(); });
    server_.onNotFound([this]() { handleNotFound(); });
}

void ConfigServer::handleRoot() {
    server_.send(200, "text/html", generateConfigPage());
}

void ConfigServer::handleSave() {
    String host = server_.arg("host");
    String portStr = server_.arg("port");
    String endpoint = server_.arg("endpoint");
    String sleepStr = server_.arg("sleep");

    uint16_t port = portStr.toInt();
    uint16_t sleep = sleepStr.toInt();

    // Validate and save
    if (host.length() > 0 && port > 0 && endpoint.length() > 0 && sleep > 0) {
        config_.setConfig(host, port, endpoint, sleep);
        server_.send(200, "text/html", generateSuccessPage("Configuration saved successfully!"));
    } else {
        server_.send(400, "text/html", generateSuccessPage("Invalid configuration. Please check all fields."));
    }
}

void ConfigServer::handleStatus() {
    // Return JSON status
    String json = "{";
    json += "\"host\":\"" + config_.getServerHost() + "\",";
    json += "\"port\":" + String(config_.getServerPort()) + ",";
    json += "\"endpoint\":\"" + config_.getImageEndpoint() + "\",";
    json += "\"sleep_minutes\":" + String(config_.getSleepMinutes()) + ",";
    json += "\"url\":\"" + config_.getFullURL() + "\"";
    json += "}";

    server_.send(200, "application/json", json);
}

void ConfigServer::handleReset() {
    config_.resetToDefaults();
    server_.send(200, "text/html", generateSuccessPage("Configuration reset to defaults."));
}

void ConfigServer::handleReboot() {
    server_.send(200, "text/html", generateSuccessPage("Rebooting... The device will restart in normal mode."));
    delay(1000);
    ESP.restart();
}

void ConfigServer::handleNotFound() {
    // In AP mode, redirect all requests to config page (captive portal)
    if (apMode_) {
        server_.sendHeader("Location", "http://192.168.4.1/", true);
        server_.send(302, "text/plain", "");
    } else {
        server_.send(404, "text/plain", "Not found");
    }
}

String ConfigServer::generateConfigPage() {
    // Get device info
    String macAddress = getMACClean();
    String ipAddress = apMode_ ? WiFi.softAPIP().toString() : WiFi.localIP().toString();

    String html = R"(
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>E-Ink Display Configuration</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
        input[type="text"], input[type="number"] { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        input[type="submit"], button { background: #007bff; color: white; padding: 12px 20px; border: none; border-radius: 4px; cursor: pointer; margin-right: 10px; margin-top: 10px; }
        input[type="submit"]:hover, button:hover { background: #0056b3; }
        .danger { background: #dc3545; }
        .danger:hover { background: #c82333; }
        .info { background: #f8f9fa; border: 1px solid #ddd; padding: 15px; border-radius: 4px; margin-top: 20px; }
        .device-info { background: #e7f3ff; border: 1px solid #b3d7ff; padding: 15px; border-radius: 4px; margin-bottom: 20px; }
        .current { color: #666; font-size: 0.9em; }
        code { background: #e9ecef; padding: 2px 6px; border-radius: 3px; font-family: monospace; }
    </style>
</head>
<body>
    <h1>E-Ink Display Setup</h1>

    <div class="device-info">
        <strong>Device Info</strong><br>
        <strong>MAC Address:</strong> <code>)";
    html += macAddress;
    html += R"(</code><br>
        <strong>IP Address:</strong> <code>)";
    html += ipAddress;
    html += R"(</code><br>
        <span class="current">Use the MAC address as the folder name on your image server for device-specific images.</span>
    </div>

    <form action="/save" method="POST">
        <div class="form-group">
            <label>Server Host (IP or domain)</label>
            <input type="text" name="host" value=")";
    html += config_.getServerHost();
    html += R"(" required>
            <span class="current">e.g., 192.168.86.100 or myserver.example.com</span>
        </div>

        <div class="form-group">
            <label>Server Port</label>
            <input type="number" name="port" value=")";
    html += String(config_.getServerPort());
    html += R"(" min="1" max="65535" required>
        </div>

        <div class="form-group">
            <label>Image Endpoint</label>
            <input type="text" name="endpoint" value=")";
    html += config_.getImageEndpoint();
    html += R"(" required>
            <span class="current">e.g., /image_packed</span>
        </div>

        <div class="form-group">
            <label>Sleep Interval (minutes)</label>
            <input type="number" name="sleep" value=")";
    html += String(config_.getSleepMinutes());
    html += R"(" min="1" max="1440" required>
            <span class="current">How often to wake and refresh (1-1440 min)</span>
        </div>

        <input type="submit" value="Save Configuration">
    </form>

    <div class="info">
        <strong>Current URL:</strong><br>
        <code>)";
    html += config_.getFullURL();
    html += R"rawliteral(</code>
    </div>

    <form action="/reset" method="POST" style="display:inline;">
        <button type="submit" class="danger" onclick="return confirm('Reset to defaults?')">Reset Defaults</button>
    </form>

    <form action="/reboot" method="POST" style="display:inline;">
        <button type="submit">Reboot Device</button>
    </form>

    <div class="info" style="margin-top: 30px;">
        <strong>Instructions:</strong><br>
        1. Enter your image server details above<br>
        2. Click "Save Configuration"<br>
        3. Click "Reboot Device" to start normal operation<br><br>
        <strong>To re-enter setup mode later:</strong><br>
        Hold Button 1, then press and release reset, and continue holding Button 1 for an additional second
    </div>
</body>
</html>
)rawliteral";
    return html;
}

String ConfigServer::generateSuccessPage(const String& message) {
    String html = R"(
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>E-Ink Display Configuration</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; background: #f5f5f5; text-align: center; }
        .message { background: #d4edda; border: 1px solid #c3e6cb; padding: 20px; border-radius: 4px; margin: 20px 0; }
        a { color: #007bff; }
    </style>
</head>
<body>
    <div class="message">)";
    html += message;
    html += R"(</div>
    <p><a href="/">Back to Configuration</a></p>
</body>
</html>
)";
    return html;
}
