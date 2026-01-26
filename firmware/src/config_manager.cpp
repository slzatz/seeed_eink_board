#include "config_manager.h"

// NVS namespace and keys
static const char* NVS_NAMESPACE = "eink_config";
static const char* KEY_HOST = "host";
static const char* KEY_PORT = "port";
static const char* KEY_ENDPOINT = "endpoint";
static const char* KEY_SLEEP = "sleep_min";

ConfigManager::ConfigManager()
    : serverHost_(DEFAULT_SERVER_HOST),
      serverPort_(DEFAULT_SERVER_PORT),
      imageEndpoint_(DEFAULT_IMAGE_ENDPOINT),
      sleepMinutes_(DEFAULT_SLEEP_MINUTES) {
}

void ConfigManager::begin() {
    loadFromNVS();
    Serial.println("ConfigManager: Initialized");
    printConfig();
}

void ConfigManager::loadFromNVS() {
    prefs_.begin(NVS_NAMESPACE, true);  // Read-only mode

    // Load with defaults if not present
    serverHost_ = prefs_.getString(KEY_HOST, DEFAULT_SERVER_HOST);
    serverPort_ = prefs_.getUShort(KEY_PORT, DEFAULT_SERVER_PORT);
    imageEndpoint_ = prefs_.getString(KEY_ENDPOINT, DEFAULT_IMAGE_ENDPOINT);
    sleepMinutes_ = prefs_.getUShort(KEY_SLEEP, DEFAULT_SLEEP_MINUTES);

    prefs_.end();
}

void ConfigManager::saveToNVS() {
    prefs_.begin(NVS_NAMESPACE, false);  // Read-write mode

    prefs_.putString(KEY_HOST, serverHost_);
    prefs_.putUShort(KEY_PORT, serverPort_);
    prefs_.putString(KEY_ENDPOINT, imageEndpoint_);
    prefs_.putUShort(KEY_SLEEP, sleepMinutes_);

    prefs_.end();
    Serial.println("ConfigManager: Saved to NVS");
}

String ConfigManager::getServerHost() {
    return serverHost_;
}

uint16_t ConfigManager::getServerPort() {
    return serverPort_;
}

String ConfigManager::getImageEndpoint() {
    return imageEndpoint_;
}

uint16_t ConfigManager::getSleepMinutes() {
    return sleepMinutes_;
}

String ConfigManager::getFullURL() {
    return String("http://") + serverHost_ + ":" + String(serverPort_) + imageEndpoint_;
}

void ConfigManager::setServerHost(const String& host) {
    if (host.length() > 0 && host.length() < MAX_HOST_LENGTH) {
        serverHost_ = host;
        saveToNVS();
    }
}

void ConfigManager::setServerPort(uint16_t port) {
    if (port > 0) {
        serverPort_ = port;
        saveToNVS();
    }
}

void ConfigManager::setImageEndpoint(const String& endpoint) {
    if (endpoint.length() > 0 && endpoint.length() < MAX_ENDPOINT_LENGTH) {
        // Ensure endpoint starts with /
        if (endpoint.charAt(0) != '/') {
            imageEndpoint_ = "/" + endpoint;
        } else {
            imageEndpoint_ = endpoint;
        }
        saveToNVS();
    }
}

void ConfigManager::setSleepMinutes(uint16_t minutes) {
    if (minutes > 0 && minutes <= 1440) {  // Max 24 hours
        sleepMinutes_ = minutes;
        saveToNVS();
    }
}

void ConfigManager::setConfig(const String& host, uint16_t port, const String& endpoint, uint16_t sleepMinutes) {
    bool changed = false;

    if (host.length() > 0 && host.length() < MAX_HOST_LENGTH) {
        serverHost_ = host;
        changed = true;
    }

    if (port > 0) {
        serverPort_ = port;
        changed = true;
    }

    if (endpoint.length() > 0 && endpoint.length() < MAX_ENDPOINT_LENGTH) {
        if (endpoint.charAt(0) != '/') {
            imageEndpoint_ = "/" + endpoint;
        } else {
            imageEndpoint_ = endpoint;
        }
        changed = true;
    }

    if (sleepMinutes > 0 && sleepMinutes <= 1440) {
        sleepMinutes_ = sleepMinutes;
        changed = true;
    }

    if (changed) {
        saveToNVS();
    }
}

void ConfigManager::resetToDefaults() {
    serverHost_ = DEFAULT_SERVER_HOST;
    serverPort_ = DEFAULT_SERVER_PORT;
    imageEndpoint_ = DEFAULT_IMAGE_ENDPOINT;
    sleepMinutes_ = DEFAULT_SLEEP_MINUTES;
    saveToNVS();
    Serial.println("ConfigManager: Reset to defaults");
}

void ConfigManager::printConfig() {
    Serial.println("Current Configuration:");
    Serial.printf("  Server: %s:%d\n", serverHost_.c_str(), serverPort_);
    Serial.printf("  Endpoint: %s\n", imageEndpoint_.c_str());
    Serial.printf("  Full URL: %s\n", getFullURL().c_str());
    Serial.printf("  Sleep: %d minutes\n", sleepMinutes_);
}
