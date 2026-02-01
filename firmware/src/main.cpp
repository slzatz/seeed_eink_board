#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
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

// Configuration mode: hold Button 1 during boot for 1 second
#define CONFIG_BUTTON_HOLD_MS 1000

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
    String hashUrl = "http://" + configManager.getServerHost() + ":" +
                     String(configManager.getServerPort()) + "/hash";

    Serial.printf("Checking image hash at: %s\n", hashUrl.c_str());
    Serial.printf("Last known hash: %s\n", lastImageHash[0] ? lastImageHash : "(none)");

    HTTPClient http;
    http.begin(hashUrl);
    http.setTimeout(HTTP_TIMEOUT_MS);

    // Add device identification header
    String macAddress = getMACAddressClean();
    http.addHeader("X-Device-MAC", macAddress);
    Serial.printf("Sending X-Device-MAC: %s\n", macAddress.c_str());

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
        return false;  // No change
    }

    // Image changed - update stored hash
    Serial.println("Image changed - will download new image");
    strncpy(lastImageHash, newHash.c_str(), 16);
    lastImageHash[16] = '\0';  // Ensure null termination

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
    http.setTimeout(HTTP_TIMEOUT_MS);

    // Add device identification header
    http.addHeader("X-Device-MAC", getMACAddressClean());

    int httpCode = http.GET();

    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("HTTP GET failed, code: %d\n", httpCode);
        free(imageBuffer);
        http.end();
        return false;
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

    while (bytesRead < contentLength && http.connected()) {
        size_t available = stream->available();
        if (available > 0) {
            size_t toRead = min(available, (size_t)(contentLength - bytesRead));
            size_t read = stream->readBytes(imageBuffer + bytesRead, toRead);
            bytesRead += read;

            // Progress update every 100KB
            if ((bytesRead % 102400) == 0) {
                Serial.printf("Downloaded: %d / %d bytes\n", bytesRead, contentLength);
            }
        }
        yield();

        // Timeout check
        if (millis() - startTime > HTTP_TIMEOUT_MS) {
            Serial.println("Download timeout!");
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

    return true;
}

void enterDeepSleep() {
    uint16_t sleepMinutes = configManager.getSleepMinutes();
    Serial.printf("Entering deep sleep for %d minutes...\n", sleepMinutes);

    // Configure timer wakeup
    uint64_t sleepTime = (uint64_t)sleepMinutes * 60 * 1000000ULL;
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

    // Connect to WiFi first (needed for hash check)
    if (!connectWiFi()) {
        Serial.println("WiFi connection failed!");
        // Keep previous image, just go to sleep
        disconnectWiFi();
        enterDeepSleep();
    }

    // Check if image has changed before downloading
    if (!checkImageChanged()) {
        Serial.println("Image unchanged - going back to sleep");
        disconnectWiFi();
        enterDeepSleep();
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
    enterDeepSleep();
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
