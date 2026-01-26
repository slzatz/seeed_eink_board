#ifndef CONFIG_SERVER_H
#define CONFIG_SERVER_H

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include "config_manager.h"

/**
 * Configuration Web Server
 *
 * Provides a web interface for configuring the device.
 * Can run in two modes:
 *   1. AP Mode - Creates its own WiFi network (for initial setup or when STA fails)
 *   2. STA Mode - Runs on existing WiFi network (for quick config changes)
 *
 * Features:
 *   - Captive portal in AP mode (auto-redirect to config page)
 *   - Simple HTML form for updating settings
 *   - REST API endpoints for programmatic configuration
 */

// AP Mode settings
#define CONFIG_AP_SSID "EInk-Setup"
#define CONFIG_AP_PASSWORD ""  // Open network for easy setup (or set a password)
#define CONFIG_AP_IP IPAddress(192, 168, 4, 1)

// DNS server for captive portal
#define DNS_PORT 53

class ConfigServer {
public:
    ConfigServer(ConfigManager& configManager);

    // Start config server in AP mode (creates own WiFi network)
    void startAPMode();

    // Start config server on existing WiFi connection
    void startSTAMode();

    // Stop the server
    void stop();

    // Handle client requests (call in loop)
    void handleClient();

    // Check if server is running
    bool isRunning() { return running_; }

    // Get the IP address to connect to
    String getIP();

private:
    ConfigManager& config_;
    WebServer server_;
    DNSServer dnsServer_;
    bool running_;
    bool apMode_;

    void setupRoutes();

    // Route handlers
    void handleRoot();
    void handleSave();
    void handleStatus();
    void handleReset();
    void handleReboot();
    void handleNotFound();

    // HTML generation
    String generateConfigPage();
    String generateSuccessPage(const String& message);
};

#endif // CONFIG_SERVER_H
