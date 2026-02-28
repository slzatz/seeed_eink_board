#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <sys/time.h>
#include <time.h>
#include "config.h"
#include "display.h"
#include "config_manager.h"
#include "config_server.h"

// Global instances
Spectra6Display display;
ConfigManager configManager;
ConfigServer configServer(configManager);

// Boot count stored in RTC memory (survives deep sleep)
RTC_DATA_ATTR int bootCount = 0;

// Last image hash stored in RTC memory (survives deep sleep)
// Used to skip download if image hasn't changed
RTC_DATA_ATTR char lastImageHash[17] = {0};  // 16 chars + null terminator
char pendingImageHash[17] = {0};

// Battery voltage (read once per boot, sent to server with requests)
float batteryVoltage = -1.0;

// Configuration mode: hold Button 1 during boot for 1 second
#define CONFIG_BUTTON_HOLD_MS 1000
#define DEVICE_CONFIG_ENDPOINT "/device_config"
#define MIN_SLEEP_SECONDS 60
#define VALID_UNIX_TIME 1704067200LL  // 2024-01-01 00:00:00 UTC

String getBaseURL() {
    return "http://" + configManager.getServerHost() + ":" + String(configManager.getServerPort());
}

/**
 * Get the WiFi MAC address as a clean string (lowercase, no separators).
 * Used to identify this device to the image server.
 */
String getMACAddressClean() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char macStr[13];
    snprintf(macStr, sizeof(macStr), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return String(macStr);
}

/**
 * Read battery voltage via the on-board voltage divider.
 * GPIO6 enables the divider circuit, GPIO1 reads the divided voltage.
 * Returns voltage in volts (e.g., 3.85), or -1.0 if reading seems invalid.
 */
float readBatteryVoltage() {
    pinMode(PIN_ADC_ENABLE, OUTPUT);
    digitalWrite(PIN_ADC_ENABLE, HIGH);
    delay(10);  // Let the ADC circuit stabilize

    analogReadResolution(12);

    // Average 16 samples to filter noise
    uint32_t sum = 0;
    for (int i = 0; i < 16; i++) {
        sum += analogRead(PIN_BATTERY_ADC);
    }
    float avgAdc = sum / 16.0;

    // Disable the voltage divider to save power
    digitalWrite(PIN_ADC_ENABLE, LOW);

    float voltage = (avgAdc / 4096.0) * BATTERY_SCALE;

    // Sanity check: LiPo range is roughly 2.5V-4.3V
    if (voltage < 0.5 || voltage > 5.0) {
        Serial.printf("Battery: ADC=%.0f, voltage=%.2fV (out of range)\n", avgAdc, voltage);
        return -1.0;
    }

    Serial.printf("Battery: ADC=%.0f, voltage=%.2fV\n", avgAdc, voltage);
    return voltage;
}

void addCommonHeaders(HTTPClient& http) {
    String macAddress = getMACAddressClean();
    http.addHeader("X-Device-MAC", macAddress);
    Serial.printf("Sending X-Device-MAC: %s\n", macAddress.c_str());

    if (batteryVoltage > 0) {
        http.addHeader("X-Battery-Voltage", String(batteryVoltage, 2));
    }
}

bool isClockValid(time_t now = time(nullptr)) {
    return now >= VALID_UNIX_TIME;
}

void setClockFromEpoch(time_t epochSeconds) {
    struct timeval tv;
    tv.tv_sec = epochSeconds;
    tv.tv_usec = 0;
    settimeofday(&tv, nullptr);
}

int32_t getLocalSecondsOfDay(time_t utcNow, int16_t timezoneOffsetMinutes) {
    int64_t localSeconds = static_cast<int64_t>(utcNow) + static_cast<int64_t>(timezoneOffsetMinutes) * 60LL;
    int32_t secondsOfDay = static_cast<int32_t>(localSeconds % 86400LL);
    if (secondsOfDay < 0) {
        secondsOfDay += 86400;
    }
    return secondsOfDay;
}

bool isWithinActiveWindow(time_t utcNow, uint8_t startHour, uint8_t endHour, int16_t timezoneOffsetMinutes) {
    if (startHour == endHour) {
        return true;  // Same start/end means always active.
    }

    int32_t secondsOfDay = getLocalSecondsOfDay(utcNow, timezoneOffsetMinutes);
    int32_t startSeconds = static_cast<int32_t>(startHour) * 3600;
    int32_t endSeconds = static_cast<int32_t>(endHour) * 3600;

    if (startHour < endHour) {
        return secondsOfDay >= startSeconds && secondsOfDay < endSeconds;
    }

    return secondsOfDay >= startSeconds || secondsOfDay < endSeconds;
}

uint32_t secondsUntilNextActiveWindow(time_t utcNow, uint8_t startHour, int16_t timezoneOffsetMinutes) {
    int32_t secondsOfDay = getLocalSecondsOfDay(utcNow, timezoneOffsetMinutes);
    int32_t startSeconds = static_cast<int32_t>(startHour) * 3600;

    if (secondsOfDay < startSeconds) {
        return static_cast<uint32_t>(startSeconds - secondsOfDay);
    }

    return static_cast<uint32_t>((86400 - secondsOfDay) + startSeconds);
}

uint32_t secondsUntilWindowEnd(time_t utcNow, uint8_t startHour, uint8_t endHour, int16_t timezoneOffsetMinutes) {
    if (startHour == endHour) {
        return UINT32_MAX;
    }

    int32_t secondsOfDay = getLocalSecondsOfDay(utcNow, timezoneOffsetMinutes);
    int32_t startSeconds = static_cast<int32_t>(startHour) * 3600;
    int32_t endSeconds = static_cast<int32_t>(endHour) * 3600;

    if (startHour < endHour) {
        return static_cast<uint32_t>(endSeconds - secondsOfDay);
    }

    if (secondsOfDay >= startSeconds) {
        return static_cast<uint32_t>((86400 - secondsOfDay) + endSeconds);
    }

    return static_cast<uint32_t>(endSeconds - secondsOfDay);
}

void printClockStatus() {
    time_t now = time(nullptr);
    if (!isClockValid(now)) {
        Serial.println("Clock status: invalid (no recent server time sync yet)");
        return;
    }

    int32_t localSeconds = getLocalSecondsOfDay(now, configManager.getTimezoneOffsetMinutes());
    int localHour = localSeconds / 3600;
    int localMinute = (localSeconds % 3600) / 60;
    bool isActive = isWithinActiveWindow(now,
                                         configManager.getActiveStartHour(),
                                         configManager.getActiveEndHour(),
                                         configManager.getTimezoneOffsetMinutes());

    Serial.printf("Clock status: utc=%lld, local=%02d:%02d, active_window=%s\n",
                  static_cast<long long>(now), localHour, localMinute,
                  isActive ? "yes" : "no");
}

uint32_t calculateSleepSeconds() {
    uint32_t refreshSeconds = static_cast<uint32_t>(configManager.getSleepMinutes()) * 60U;
    time_t now = time(nullptr);

    if (!isClockValid(now)) {
        Serial.println("Clock invalid - using fixed refresh interval for sleep");
        return max(refreshSeconds, static_cast<uint32_t>(MIN_SLEEP_SECONDS));
    }

    uint8_t activeStart = configManager.getActiveStartHour();
    uint8_t activeEnd = configManager.getActiveEndHour();
    int16_t timezoneOffset = configManager.getTimezoneOffsetMinutes();

    if (!isWithinActiveWindow(now, activeStart, activeEnd, timezoneOffset)) {
        uint32_t untilNextWindow = secondsUntilNextActiveWindow(now, activeStart, timezoneOffset);
        Serial.printf("Outside active window - sleeping until next active start in %lu seconds\n", untilNextWindow);
        return max(untilNextWindow, static_cast<uint32_t>(MIN_SLEEP_SECONDS));
    }

    uint32_t untilWindowEnd = secondsUntilWindowEnd(now, activeStart, activeEnd, timezoneOffset);
    if (refreshSeconds < untilWindowEnd) {
        return max(refreshSeconds, static_cast<uint32_t>(MIN_SLEEP_SECONDS));
    }

    uint32_t untilNextWindow = secondsUntilNextActiveWindow(now, activeStart, timezoneOffset);
    Serial.printf("Next refresh would land in quiet hours - sleeping %lu seconds instead\n", untilNextWindow);
    return max(untilNextWindow, static_cast<uint32_t>(MIN_SLEEP_SECONDS));
}

bool syncRemoteConfigAndTime() {
    String configUrl = getBaseURL() + DEVICE_CONFIG_ENDPOINT;
    Serial.printf("Fetching device config from: %s\n", configUrl.c_str());

    HTTPClient http;
    http.begin(configUrl);
    http.setTimeout(HTTP_TIMEOUT_MS);
    addCommonHeaders(http);

    int httpCode = http.GET();
    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("Device config fetch failed, HTTP code: %d\n", httpCode);
        http.end();
        return false;
    }

    String payload = http.getString();
    http.end();

    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);
    if (error) {
        Serial.printf("Failed to parse device config JSON: %s\n", error.c_str());
        return false;
    }

    if (!doc.containsKey("server_time_epoch")) {
        Serial.println("Device config missing server_time_epoch");
        return false;
    }

    time_t serverEpoch = static_cast<time_t>(doc["server_time_epoch"].as<int64_t>());
    setClockFromEpoch(serverEpoch);
    Serial.printf("Clock synchronized from server epoch: %lld\n", static_cast<long long>(serverEpoch));

    uint16_t refreshMinutes = configManager.getSleepMinutes();
    uint8_t activeStart = configManager.getActiveStartHour();
    uint8_t activeEnd = configManager.getActiveEndHour();
    int16_t timezoneOffset = configManager.getTimezoneOffsetMinutes();
    bool scheduleChanged = false;

    if (doc.containsKey("refresh_interval_minutes")) {
        int value = doc["refresh_interval_minutes"].as<int>();
        if (value > 0 && value <= 1440 && value != refreshMinutes) {
            refreshMinutes = static_cast<uint16_t>(value);
            scheduleChanged = true;
        }
    }

    if (doc.containsKey("active_start_hour")) {
        int value = doc["active_start_hour"].as<int>();
        if (value >= 0 && value <= 23 && value != activeStart) {
            activeStart = static_cast<uint8_t>(value);
            scheduleChanged = true;
        }
    }

    if (doc.containsKey("active_end_hour")) {
        int value = doc["active_end_hour"].as<int>();
        if (value >= 0 && value <= 23 && value != activeEnd) {
            activeEnd = static_cast<uint8_t>(value);
            scheduleChanged = true;
        }
    }

    if (doc.containsKey("timezone_offset_minutes")) {
        int value = doc["timezone_offset_minutes"].as<int>();
        if (value >= -720 && value <= 840 && value != timezoneOffset) {
            timezoneOffset = static_cast<int16_t>(value);
            scheduleChanged = true;
        }
    }

    if (scheduleChanged) {
        configManager.setConfig(configManager.getServerHost(),
                                configManager.getServerPort(),
                                configManager.getImageEndpoint(),
                                refreshMinutes,
                                activeStart,
                                activeEnd,
                                timezoneOffset);
        Serial.println("Applied schedule overrides from server");
        configManager.printConfig();
    }

    const char* configSource = doc["config_source"] | "none";
    Serial.printf("Remote config source: %s\n", configSource);
    return true;
}

void printWakeupReason() {
    esp_sleep_wakeup_cause_t wakeupReason = esp_sleep_get_wakeup_cause();
    switch (wakeupReason) {
        case ESP_SLEEP_WAKEUP_TIMER:
            Serial.println("Wakeup caused by timer");
            break;
        case ESP_SLEEP_WAKEUP_EXT0:
            Serial.println("Wakeup caused by external signal (RTC_IO)");
            break;
        case ESP_SLEEP_WAKEUP_EXT1:
            Serial.println("Wakeup caused by external signal (RTC_CNTL)");
            break;
        default:
            Serial.printf("Wakeup was not from deep sleep (code: %d)\n", wakeupReason);
            break;
    }
}

bool checkConfigButton() {
    // Configure button pin with internal pull-up
    pinMode(PIN_BUTTON_1, INPUT_PULLUP);

    // Check if button is pressed (LOW = pressed)
    if (digitalRead(PIN_BUTTON_1) == LOW) {
        Serial.println("Config button pressed - hold for 1 second to enter config mode...");

        // Wait and check if button is held for the required duration
        uint32_t startTime = millis();
        while (digitalRead(PIN_BUTTON_1) == LOW) {
            if (millis() - startTime >= CONFIG_BUTTON_HOLD_MS) {
                Serial.println("*** CONFIG BUTTON HELD - Entering config mode ***");
                return true;
            }
            delay(50);
        }
        Serial.println("Button released too early - continuing normal operation");
    }

    return false;
}

bool connectWiFi() {
    Serial.printf("Connecting to WiFi: %s\n", WIFI_SSID);

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    uint32_t startTime = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");

        if (millis() - startTime > WIFI_TIMEOUT_MS) {
            Serial.println("\nWiFi connection timeout!");
            return false;
        }
    }

    Serial.println();
    Serial.printf("Connected! IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
}

void disconnectWiFi() {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    Serial.println("WiFi disconnected");
}

/**
 * Check if the image on the server has changed by comparing hashes.
 *
 * @return true if image has changed (or if check failed), false if unchanged
 */
bool checkImageChanged() {
    // Build hash endpoint URL from config
    String hashUrl = getBaseURL() + "/hash";

    Serial.printf("Checking image hash at: %s\n", hashUrl.c_str());
    Serial.printf("Last known hash: %s\n", lastImageHash[0] ? lastImageHash : "(none)");

    HTTPClient http;
    http.begin(hashUrl);
    http.setTimeout(HTTP_TIMEOUT_MS);
    addCommonHeaders(http);

    int httpCode = http.GET();

    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("Hash check failed, HTTP code: %d\n", httpCode);
        http.end();
        return true;  // Assume changed if we can't check
    }

    String newHash = http.getString();
    http.end();

    // Validate hash length (should be 16 characters)
    if (newHash.length() != 16) {
        Serial.printf("Invalid hash length: %d\n", newHash.length());
        return true;  // Assume changed if invalid
    }

    Serial.printf("Server hash: %s\n", newHash.c_str());

    // Compare with stored hash
    if (strcmp(newHash.c_str(), lastImageHash) == 0) {
        Serial.println("Image unchanged - skipping download");
        pendingImageHash[0] = '\0';
        return false;  // No change
    }

    Serial.println("Image changed - will download new image");
    strncpy(pendingImageHash, newHash.c_str(), 16);
    pendingImageHash[16] = '\0';

    return true;  // Changed
}

bool fetchAndDisplayImage() {
    // Allocate buffer in PSRAM for the image
    uint8_t* imageBuffer = (uint8_t*)ps_malloc(BUFFER_SIZE);
    if (imageBuffer == nullptr) {
        Serial.println("Failed to allocate image buffer!");
        return false;
    }

    // Get URL from config manager
    String url = configManager.getFullURL();
    Serial.printf("Fetching image from: %s\n", url.c_str());

    HTTPClient http;
    http.begin(url);
    http.setTimeout(IMAGE_HTTP_TIMEOUT_MS);
    addCommonHeaders(http);

    int httpCode = http.GET();

    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("HTTP GET failed, code: %d\n", httpCode);
        free(imageBuffer);
        http.end();
        return false;
    }

    String responseImageHash = http.header("X-Image-Hash");
    String responseImageName = http.header("X-Image-Name");
    String responseDeviceId = http.header("X-Device-ID");
    if (responseImageName.length() > 0 || responseImageHash.length() > 0 || responseDeviceId.length() > 0) {
        Serial.printf("Response headers: X-Image-Name=%s, X-Image-Hash=%s, X-Device-ID=%s\n",
                      responseImageName.length() > 0 ? responseImageName.c_str() : "(none)",
                      responseImageHash.length() > 0 ? responseImageHash.c_str() : "(none)",
                      responseDeviceId.length() > 0 ? responseDeviceId.c_str() : "(none)");
    }

    int contentLength = http.getSize();
    Serial.printf("Content length: %d bytes\n", contentLength);

    if (contentLength <= 0 || contentLength > BUFFER_SIZE) {
        Serial.printf("Invalid content length: %d (expected %d)\n", contentLength, BUFFER_SIZE);
        free(imageBuffer);
        http.end();
        return false;
    }

    // Stream the response directly into our buffer
    WiFiClient* stream = http.getStreamPtr();
    size_t bytesRead = 0;
    uint32_t startTime = millis();
    uint32_t lastDataTime = startTime;

    while (bytesRead < contentLength && http.connected()) {
        size_t available = stream->available();
        if (available > 0) {
            size_t toRead = min(available, (size_t)(contentLength - bytesRead));
            size_t read = stream->readBytes(imageBuffer + bytesRead, toRead);
            bytesRead += read;
            lastDataTime = millis();

            // Progress update every 100KB
            if ((bytesRead % 102400) == 0) {
                Serial.printf("Downloaded: %d / %d bytes\n", bytesRead, contentLength);
            }
        }
        yield();

        // Treat the timeout as "no data received recently", not total transfer duration.
        if (millis() - lastDataTime > IMAGE_HTTP_TIMEOUT_MS) {
            Serial.println("Download stalled - inactivity timeout");
            break;
        }
    }

    http.end();

    Serial.printf("Downloaded %d bytes in %lu ms\n", bytesRead, millis() - startTime);

    if (bytesRead != contentLength) {
        Serial.println("Incomplete download!");
        free(imageBuffer);
        return false;
    }

    // Load image data into display buffer
    display.loadImageData(imageBuffer, bytesRead);

    // Free the temporary buffer
    free(imageBuffer);

    // Refresh the display
    display.refresh();

    if (responseImageHash.length() == 16) {
        strncpy(lastImageHash, responseImageHash.c_str(), 16);
        lastImageHash[16] = '\0';
    } else if (pendingImageHash[0] != '\0') {
        strncpy(lastImageHash, pendingImageHash, 16);
        lastImageHash[16] = '\0';
    }

    if (pendingImageHash[0] != '\0' && responseImageHash.length() == 16 &&
        strcmp(pendingImageHash, responseImageHash.c_str()) != 0) {
        Serial.printf("Warning: pending hash %s did not match response hash %s\n",
                      pendingImageHash, responseImageHash.c_str());
    }
    pendingImageHash[0] = '\0';
    Serial.printf("Committed displayed image hash: %s\n", lastImageHash[0] ? lastImageHash : "(none)");

    return true;
}

void enterDeepSleep(uint32_t sleepSeconds) {
    uint32_t sleepMinutes = sleepSeconds / 60;
    uint32_t remainderSeconds = sleepSeconds % 60;
    Serial.printf("Entering deep sleep for %lu minutes %lu seconds...\n", sleepMinutes, remainderSeconds);

    // Configure timer wakeup
    uint64_t sleepTime = static_cast<uint64_t>(sleepSeconds) * 1000000ULL;
    esp_sleep_enable_timer_wakeup(sleepTime);

    // Turn off display power to save energy
    digitalWrite(PIN_POWER, LOW);

    // Enter deep sleep
    Serial.println("Going to sleep now...");
    Serial.flush();
    esp_deep_sleep_start();
}

void runConfigMode() {
    Serial.println("\n========================================");
    Serial.println("CONFIGURATION MODE");
    Serial.println("========================================\n");

    // Try to connect to WiFi first for STA mode config
    if (connectWiFi()) {
        Serial.println("\nWiFi connected - starting config server in STA mode");
        Serial.printf("Open http://%s in your browser to configure\n", WiFi.localIP().toString().c_str());
        configServer.startSTAMode();
    } else {
        Serial.println("\nWiFi connection failed - starting AP mode");
        Serial.printf("Connect to WiFi network '%s' and open http://192.168.4.1\n", CONFIG_AP_SSID);
        configServer.startAPMode();
    }

    // Run config server indefinitely until user reboots
    while (true) {
        configServer.handleClient();
        delay(10);
    }
}

void runNormalMode() {
    Serial.println("\n========================================");
    Serial.println("NORMAL OPERATION MODE");
    Serial.println("========================================\n");

    // Read battery voltage before WiFi (ADC can be noisy during WiFi)
    batteryVoltage = readBatteryVoltage();

    // Connect to WiFi first (needed for hash check)
    if (!connectWiFi()) {
        Serial.println("WiFi connection failed!");
        // Keep previous image, just go to sleep
        disconnectWiFi();
        enterDeepSleep(calculateSleepSeconds());
    }

    syncRemoteConfigAndTime();
    printClockStatus();

    if (isClockValid() &&
        !isWithinActiveWindow(time(nullptr),
                              configManager.getActiveStartHour(),
                              configManager.getActiveEndHour(),
                              configManager.getTimezoneOffsetMinutes())) {
        Serial.println("Currently in quiet hours - skipping hash/image fetch");
        disconnectWiFi();
        enterDeepSleep(calculateSleepSeconds());
    }

    // Check if image has changed before downloading
    if (!checkImageChanged()) {
        Serial.println("Image unchanged - going back to sleep");
        disconnectWiFi();
        enterDeepSleep(calculateSleepSeconds());
    }

    // Image has changed - initialize display and update
    if (!display.begin()) {
        Serial.println("Display initialization failed!");
        disconnectWiFi();
        delay(5000);
        ESP.restart();
    }

    // Fetch and display image
    if (!fetchAndDisplayImage()) {
        Serial.println("Image fetch/display failed!");
        // Keep previous image on display
    }

    // Disconnect WiFi to save power
    disconnectWiFi();

    // Enter deep sleep
    enterDeepSleep(calculateSleepSeconds());
}

void setup() {
    Serial.begin(115200);
    delay(1000);  // Give serial time to connect

    Serial.println("\n========================================");
    Serial.println("Seeed EE02 E-Ink Display Firmware");
    Serial.println("========================================");

    bootCount++;
    Serial.printf("Boot count: %d\n", bootCount);
    printWakeupReason();

    // Initialize configuration manager
    configManager.begin();

    // Check if config button (Button 1 / GPIO2) is held to enter config mode
    if (checkConfigButton()) {
        runConfigMode();
        // runConfigMode never returns
    }

    // Normal operation
    runNormalMode();
}

void loop() {
    // This should never be reached due to deep sleep
    delay(1000);
}
