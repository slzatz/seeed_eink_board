#ifndef CONFIG_MANAGER_H
#define CONFIG_MANAGER_H

#include <Arduino.h>
#include <Preferences.h>

/**
 * Configuration Manager
 *
 * Handles persistent storage of configuration values in NVS (Non-Volatile Storage).
 * Values persist across reboots and deep sleep cycles.
 *
 * Stored configuration:
 *   - Server host (e.g., "192.168.86.100" or "myserver.example.com")
 *   - Server port (e.g., 5000)
 *   - Image endpoint path (e.g., "/image_packed")
 *   - Refresh interval in minutes
 *   - Active window start/end hour (0-23, local time)
 *   - Local timezone offset from UTC in minutes
 */

// Default values (used on first boot or after NVS reset)
#define DEFAULT_SERVER_HOST "192.168.86.34"
#define DEFAULT_SERVER_PORT 5000
#define DEFAULT_IMAGE_ENDPOINT "/image_packed"
#define DEFAULT_SLEEP_MINUTES 15
#define DEFAULT_ACTIVE_START_HOUR 8
#define DEFAULT_ACTIVE_END_HOUR 20
#define DEFAULT_TIMEZONE_OFFSET_MINUTES 0

// Maximum string lengths
#define MAX_HOST_LENGTH 128
#define MAX_ENDPOINT_LENGTH 64

class ConfigManager {
public:
    ConfigManager();

    // Initialize and load config from NVS
    void begin();

    // Get current configuration
    String getServerHost();
    uint16_t getServerPort();
    String getImageEndpoint();
    uint16_t getSleepMinutes();
    uint8_t getActiveStartHour();
    uint8_t getActiveEndHour();
    int16_t getTimezoneOffsetMinutes();

    // Build full URL from components
    String getFullURL();

    // Set configuration (automatically saves to NVS)
    void setServerHost(const String& host);
    void setServerPort(uint16_t port);
    void setImageEndpoint(const String& endpoint);
    void setSleepMinutes(uint16_t minutes);
    void setActiveStartHour(uint8_t hour);
    void setActiveEndHour(uint8_t hour);
    void setTimezoneOffsetMinutes(int16_t minutes);

    // Set all at once
    void setConfig(const String& host, uint16_t port, const String& endpoint,
                   uint16_t sleepMinutes, uint8_t activeStartHour,
                   uint8_t activeEndHour, int16_t timezoneOffsetMinutes);

    // Reset to defaults
    void resetToDefaults();

    // Print current config to Serial
    void printConfig();

private:
    Preferences prefs_;
    String serverHost_;
    uint16_t serverPort_;
    String imageEndpoint_;
    uint16_t sleepMinutes_;
    uint8_t activeStartHour_;
    uint8_t activeEndHour_;
    int16_t timezoneOffsetMinutes_;

    void loadFromNVS();
    void saveToNVS();
};

#endif // CONFIG_MANAGER_H
