#include "display.h"

// Initialization data from Seeed's T133A01_Defines.h (via esphome-bigink)

// Commands to MASTER only
static const uint8_t R74_DATA[] = {0xC0, 0x1C, 0x1C, 0xCC, 0xCC, 0xCC, 0x15, 0x15, 0x55};
static const uint8_t PWR_DATA[] = {0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38};
static const uint8_t RB6_DATA[] = {0x07};
static const uint8_t BTST_P_DATA[] = {0xD8, 0x18};
static const uint8_t RB7_DATA[] = {0x01};
static const uint8_t BTST_N_DATA[] = {0xD8, 0x18};
static const uint8_t RB0_DATA[] = {0x01};
static const uint8_t RB1_DATA[] = {0x02};

// Commands to SLAVE only (via BOTH pattern)
static const uint8_t RF0_DATA[] = {0x49, 0x55, 0x13, 0x5D, 0x05, 0x10};
static const uint8_t PSR_DATA[] = {0xDF, 0x69};
static const uint8_t CDI_DATA[] = {0x37};
static const uint8_t R60_DATA[] = {0x03, 0x03};
static const uint8_t R86_DATA[] = {0x10};
static const uint8_t PWS_DATA[] = {0x22};
static const uint8_t TRES_DATA[] = {0x04, 0xB0, 0x03, 0x20};  // Resolution: 0x04B0=1200, 0x0320=800 (per controller)

// Refresh commands
static const uint8_t POF_DATA[] = {0x00};
static const uint8_t SLEEP_DATA[] = {0xA5};

Spectra6Display::Spectra6Display()
    : buffer_(nullptr), spiInitialized_(false) {
}

bool Spectra6Display::begin() {
    Serial.println("Spectra6: Initializing display...");

    // Allocate frame buffer in PSRAM
    if (psramFound()) {
        buffer_ = (uint8_t*)ps_malloc(BUFFER_SIZE);
        if (buffer_) {
            Serial.printf("Spectra6: Buffer allocated in PSRAM (%d bytes)\n", BUFFER_SIZE);
        }
    }

    if (buffer_ == nullptr) {
        buffer_ = (uint8_t*)malloc(BUFFER_SIZE);
        if (buffer_) {
            Serial.println("Spectra6: Buffer allocated in regular RAM");
        }
    }

    if (buffer_ == nullptr) {
        Serial.println("Spectra6: ERROR - Failed to allocate frame buffer!");
        return false;
    }

    // Clear buffer to white
    memset(buffer_, 0x11, BUFFER_SIZE);  // 0x11 = two white pixels

    // Configure GPIO pins
    pinMode(PIN_CS_MASTER, OUTPUT);
    digitalWrite(PIN_CS_MASTER, HIGH);

    pinMode(PIN_CS_SLAVE, OUTPUT);
    digitalWrite(PIN_CS_SLAVE, HIGH);

    pinMode(PIN_DC, OUTPUT);
    digitalWrite(PIN_DC, LOW);

    pinMode(PIN_RESET, OUTPUT);
    digitalWrite(PIN_RESET, HIGH);

    pinMode(PIN_BUSY, INPUT);

    pinMode(PIN_POWER, OUTPUT);
    digitalWrite(PIN_POWER, HIGH);  // Power on display

    Serial.println("Spectra6: GPIO configured");

    // Hardware reset and initialize
    hardwareReset();
    initializeDisplay();

    Serial.println("Spectra6: Display initialized successfully");
    return true;
}

void Spectra6Display::loadImageData(const uint8_t* data, size_t length) {
    if (buffer_ == nullptr || data == nullptr) return;

    size_t copyLen = (length > BUFFER_SIZE) ? BUFFER_SIZE : length;
    memcpy(buffer_, data, copyLen);
    Serial.printf("Spectra6: Loaded %d bytes of image data\n", copyLen);
}

void Spectra6Display::refresh() {
    Serial.println("Spectra6: Starting display refresh...");
    uint32_t startTime = millis();

    // Re-initialize display before transfer
    hardwareReset();
    initializeDisplay();

    // Transfer data to both controllers
    transferData();

    // Refresh the display
    refreshScreen();

    // Power off and sleep
    powerOff();
    displaySleep();

    Serial.printf("Spectra6: Refresh complete in %lu ms\n", millis() - startTime);
}

void Spectra6Display::fillColor(uint8_t color) {
    if (buffer_ == nullptr) return;
    uint8_t byteVal = (color << 4) | color;
    memset(buffer_, byteVal, BUFFER_SIZE);
}

void Spectra6Display::sleep() {
    displaySleep();
}

// ============================================================================
// Hardware Control
// ============================================================================

void Spectra6Display::hardwareReset() {
    Serial.println("Spectra6: Hardware reset");
    digitalWrite(PIN_RESET, LOW);
    delay(10);
    digitalWrite(PIN_RESET, HIGH);
    delay(10);
    waitUntilIdle(2000);
}

bool Spectra6Display::waitUntilIdle(uint32_t timeoutMs) {
    uint32_t start = millis();
    // Note: Busy pin on EE02 is inverted - reads LOW when busy, HIGH when ready
    while (digitalRead(PIN_BUSY) == LOW) {
        delay(10);
        if (millis() - start > timeoutMs) {
            Serial.println("Spectra6: Wait timeout");
            return false;
        }
    }
    return true;
}

// ============================================================================
// SPI Operations
// ============================================================================

void Spectra6Display::spiBegin() {
    if (!spiInitialized_) {
        SPI.begin(PIN_SPI_CLK, -1, PIN_SPI_MOSI, -1);
        spiInitialized_ = true;
    }
    SPI.beginTransaction(SPISettings(10000000, MSBFIRST, SPI_MODE0));
}

void Spectra6Display::spiEnd() {
    SPI.endTransaction();
}

void Spectra6Display::spiWriteByte(uint8_t data) {
    SPI.transfer(data);
}

void Spectra6Display::spiWriteArray(const uint8_t* data, size_t len) {
    SPI.transferBytes(data, nullptr, len);
}

// ============================================================================
// Dual-Controller Commands
// ============================================================================

void Spectra6Display::bothCommand(uint8_t cmd) {
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    digitalWrite(PIN_CS_SLAVE, LOW);
    spiWriteByte(cmd);
    digitalWrite(PIN_CS_MASTER, HIGH);
    digitalWrite(PIN_CS_SLAVE, HIGH);
    spiEnd();
}

void Spectra6Display::bothCmdData(uint8_t cmd, const uint8_t* data, size_t len) {
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    digitalWrite(PIN_CS_SLAVE, LOW);
    spiWriteByte(cmd);
    if (len > 0 && data != nullptr) {
        digitalWrite(PIN_DC, HIGH);
        spiWriteArray(data, len);
    }
    digitalWrite(PIN_CS_MASTER, HIGH);
    digitalWrite(PIN_CS_SLAVE, HIGH);
    spiEnd();
}

void Spectra6Display::masterCommand(uint8_t cmd) {
    digitalWrite(PIN_CS_SLAVE, HIGH);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(cmd);
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();
}

void Spectra6Display::masterCmdData(uint8_t cmd, const uint8_t* data, size_t len) {
    digitalWrite(PIN_CS_SLAVE, HIGH);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(cmd);
    if (len > 0 && data != nullptr) {
        digitalWrite(PIN_DC, HIGH);
        spiWriteArray(data, len);
    }
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();
}

void Spectra6Display::slaveCommand(uint8_t cmd) {
    digitalWrite(PIN_CS_MASTER, HIGH);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_SLAVE, LOW);
    spiWriteByte(cmd);
    digitalWrite(PIN_CS_SLAVE, HIGH);
    spiEnd();
}

void Spectra6Display::slaveCmdData(uint8_t cmd, const uint8_t* data, size_t len) {
    digitalWrite(PIN_CS_MASTER, HIGH);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_SLAVE, LOW);
    spiWriteByte(cmd);
    if (len > 0 && data != nullptr) {
        digitalWrite(PIN_DC, HIGH);
        spiWriteArray(data, len);
    }
    digitalWrite(PIN_CS_SLAVE, HIGH);
    spiEnd();
}

void Spectra6Display::sendCmdDataWithCS(uint8_t cmd, const uint8_t* data, size_t len) {
    // Helper: send command+data while toggling only master CS (slave CS controlled by caller)
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(cmd);
    if (len > 0 && data != nullptr) {
        digitalWrite(PIN_DC, HIGH);
        spiWriteArray(data, len);
    }
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();
}

// ============================================================================
// Initialization Sequence
// ============================================================================

void Spectra6Display::initializeDisplay() {
    Serial.println("Spectra6: Running initialization sequence...");

    // 0x74 to MASTER only
    masterCmdData(0x74, R74_DATA, sizeof(R74_DATA));

    // 0xF0 to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    delay(10);
    sendCmdDataWithCS(0xF0, RF0_DATA, sizeof(RF0_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // PSR (0x00) to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    sendCmdDataWithCS(0x00, PSR_DATA, sizeof(PSR_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // CDI (0x50) to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    sendCmdDataWithCS(0x50, CDI_DATA, sizeof(CDI_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // 0x60 to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    sendCmdDataWithCS(0x60, R60_DATA, sizeof(R60_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // 0x86 to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    sendCmdDataWithCS(0x86, R86_DATA, sizeof(R86_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // PWS (0xE3) to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    sendCmdDataWithCS(0xE3, PWS_DATA, sizeof(PWS_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // TRES (0x61) to BOTH
    digitalWrite(PIN_CS_SLAVE, LOW);
    sendCmdDataWithCS(0x61, TRES_DATA, sizeof(TRES_DATA));
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(10);

    // Remaining commands to MASTER only
    masterCmdData(0x01, PWR_DATA, sizeof(PWR_DATA));
    delay(10);
    masterCmdData(0xB6, RB6_DATA, sizeof(RB6_DATA));
    delay(10);
    masterCmdData(0x06, BTST_P_DATA, sizeof(BTST_P_DATA));
    delay(10);
    masterCmdData(0xB7, RB7_DATA, sizeof(RB7_DATA));
    delay(10);
    masterCmdData(0x05, BTST_N_DATA, sizeof(BTST_N_DATA));
    delay(10);
    masterCmdData(0xB0, RB0_DATA, sizeof(RB0_DATA));
    delay(10);
    masterCmdData(0xB1, RB1_DATA, sizeof(RB1_DATA));
    delay(10);

    Serial.println("Spectra6: Initialization complete");
}

// ============================================================================
// Data Transfer
// ============================================================================

uint8_t Spectra6Display::getPixel(uint16_t x, uint16_t y) {
    size_t byteIdx = ((size_t)y * DISPLAY_WIDTH + x) / 2;
    uint8_t b = buffer_[byteIdx];
    return (x & 1) ? (b & 0x0F) : ((b >> 4) & 0x0F);
}

void Spectra6Display::transferData() {
    Serial.println("Spectra6: Starting data transfer...");
    uint32_t start = millis();

    const uint16_t OUT_ROWS = 1600;   // All buffer columns become output rows
    const uint16_t OUT_BYTES = 300;   // 600 buffer rows / 2 = 300 bytes per output row

    // CCSET (0xE0) to BOTH controllers
    digitalWrite(PIN_CS_SLAVE, LOW);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(0xE0);
    digitalWrite(PIN_DC, HIGH);
    spiWriteByte(0x01);
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();
    digitalWrite(PIN_CS_SLAVE, HIGH);

    waitUntilIdle(1000);
    delay(10);

    // === MASTER: All columns, top half of rows (0-599) ===
    Serial.println("Spectra6: Sending to MASTER...");

    digitalWrite(PIN_CS_SLAVE, HIGH);
    digitalWrite(PIN_CS_MASTER, LOW);
    spiBegin();
    digitalWrite(PIN_DC, LOW);
    spiWriteByte(0x10);  // DTM
    digitalWrite(PIN_DC, HIGH);

    uint32_t masterStart = millis();
    for (uint16_t outRow = 0; outRow < OUT_ROWS; outRow++) {
        // Transpose: buffer column becomes output row (with FLIP reversal)
        uint16_t bufCol = 1599 - outRow;

        for (uint16_t outByte = 0; outByte < OUT_BYTES; outByte++) {
            // Top half: buffer rows 0-599
            uint16_t bufRowEven = 2 * outByte;
            uint16_t bufRowOdd = 2 * outByte + 1;

            uint8_t pixEven = getPixel(bufCol, bufRowEven);
            uint8_t pixOdd = getPixel(bufCol, bufRowOdd);

            SPI.transfer((pixEven << 4) | pixOdd);
        }

        // Feed watchdog periodically
        if ((outRow & 0xFF) == 0) yield();
    }

    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();
    Serial.printf("Spectra6: Master data sent in %lu ms\n", millis() - masterStart);

    // === SLAVE: All columns, bottom half of rows (600-1199) ===
    Serial.println("Spectra6: Sending to SLAVE...");

    digitalWrite(PIN_CS_MASTER, HIGH);
    digitalWrite(PIN_CS_SLAVE, LOW);
    spiBegin();
    digitalWrite(PIN_DC, LOW);
    spiWriteByte(0x10);  // DTM
    digitalWrite(PIN_DC, HIGH);

    uint32_t slaveStart = millis();
    for (uint16_t outRow = 0; outRow < OUT_ROWS; outRow++) {
        // Transpose: buffer column becomes output row (with FLIP reversal)
        uint16_t bufCol = 1599 - outRow;

        for (uint16_t outByte = 0; outByte < OUT_BYTES; outByte++) {
            // Bottom half: buffer rows 600-1199
            uint16_t bufRowEven = 600 + 2 * outByte;
            uint16_t bufRowOdd = 600 + 2 * outByte + 1;

            uint8_t pixEven = getPixel(bufCol, bufRowEven);
            uint8_t pixOdd = getPixel(bufCol, bufRowOdd);

            SPI.transfer((pixEven << 4) | pixOdd);
        }

        // Feed watchdog periodically
        if ((outRow & 0xFF) == 0) yield();
    }

    digitalWrite(PIN_CS_SLAVE, HIGH);
    spiEnd();

    Serial.printf("Spectra6: Slave data sent in %lu ms\n", millis() - slaveStart);
    Serial.printf("Spectra6: Data transfer complete in %lu ms\n", millis() - start);
}

// ============================================================================
// Refresh Sequence
// ============================================================================

void Spectra6Display::refreshScreen() {
    Serial.println("Spectra6: Starting refresh sequence...");

    // Power ON (0x04)
    digitalWrite(PIN_CS_SLAVE, LOW);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(0x04);
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();

    waitUntilIdle(5000);
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(30);

    // Refresh (0x12)
    Serial.println("Spectra6: Sending refresh command (this takes 20-30 seconds)...");
    digitalWrite(PIN_CS_SLAVE, LOW);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(0x12);
    digitalWrite(PIN_DC, HIGH);
    spiWriteByte(0x01);
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();

    uint32_t refreshStart = millis();
    if (!waitUntilIdle(60000)) {
        Serial.println("Spectra6: Refresh timeout");
    } else {
        Serial.printf("Spectra6: Refresh complete in %lu ms\n", millis() - refreshStart);
    }

    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(30);
}

void Spectra6Display::powerOff() {
    Serial.println("Spectra6: Power off");

    digitalWrite(PIN_CS_SLAVE, LOW);
    digitalWrite(PIN_DC, LOW);
    spiBegin();
    digitalWrite(PIN_CS_MASTER, LOW);
    spiWriteByte(0x02);
    digitalWrite(PIN_DC, HIGH);
    spiWriteByte(0x00);
    digitalWrite(PIN_CS_MASTER, HIGH);
    spiEnd();

    waitUntilIdle(5000);
    digitalWrite(PIN_CS_SLAVE, HIGH);
    delay(30);
}

void Spectra6Display::displaySleep() {
    Serial.println("Spectra6: Entering deep sleep");
    bothCmdData(0x07, SLEEP_DATA, sizeof(SLEEP_DATA));
}
