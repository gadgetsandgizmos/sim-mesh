/**
 * model — see the header.
 *
 * Everything that touches a chip's state does so under the services' lock,
 * because the bus caller reaches it from one side and the reader and the timer
 * thread reach it from the other. The DIO1 line is driven outside that lock:
 * raising it runs the host's pin callback on the calling thread, and a host's
 * interrupt handler issues SPI commands of its own.
 */
#include "simradio.h"

#include "busy.h"
#include "conductor.h"
#include "ether_link.h"
#include "json.h"
#include "model.h"
#include "services.h"
#include "toa.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <algorithm>
#include <sys/random.h>
#include <atomic>
#include <map>
#include <mutex>
#include <string>
#include <vector>

/* ---- The SX126x command and register surface the drivers use ---- */

enum {
    CMD_RESET_STATS        = 0x00,
    CMD_CLEAR_IRQ_STATUS   = 0x02,
    CMD_CLEAR_DEVICE_ERR   = 0x07,
    CMD_SET_DIO_IRQ_PARAMS = 0x08,
    CMD_WRITE_REGISTER     = 0x0D,
    CMD_WRITE_BUFFER       = 0x0E,
    CMD_GET_STATS          = 0x10,
    CMD_GET_PACKET_TYPE    = 0x11,
    CMD_GET_IRQ_STATUS     = 0x12,
    CMD_GET_RX_BUF_STATUS  = 0x13,
    CMD_GET_PACKET_STATUS  = 0x14,
    CMD_GET_RSSI_INST      = 0x15,
    CMD_GET_DEVICE_ERRORS  = 0x17,
    CMD_READ_REGISTER      = 0x1D,
    CMD_READ_BUFFER        = 0x1E,
    CMD_SET_STANDBY        = 0x80,
    CMD_SET_RX             = 0x82,
    CMD_SET_TX             = 0x83,
    CMD_SET_SLEEP          = 0x84,
    CMD_SET_RF_FREQUENCY   = 0x86,
    CMD_SET_CAD_PARAMS     = 0x88,
    CMD_CALIBRATE          = 0x89,
    CMD_SET_PACKET_TYPE    = 0x8A,
    CMD_SET_MODULATION     = 0x8B,
    CMD_SET_PACKET_PARAMS  = 0x8C,
    CMD_SET_TX_PARAMS      = 0x8E,
    CMD_SET_BUFFER_BASE    = 0x8F,
    CMD_SET_RXTX_FALLBACK  = 0x93,
    CMD_SET_PA_CONFIG      = 0x95,
    CMD_SET_REGULATOR      = 0x96,
    CMD_SET_DIO3_TCXO      = 0x97,
    CMD_CALIBRATE_IMAGE    = 0x98,
    CMD_SET_DIO2_RF_SWITCH = 0x9D,
    CMD_STOP_TIMER_ON_PRE  = 0x9F,
    CMD_SET_LORA_SYMB_TO   = 0xA0,
    CMD_GET_STATUS         = 0xC0,
    CMD_SET_FS             = 0xC1,
    CMD_SET_CAD            = 0xC5,
};

enum {
    IRQ_TX_DONE           = 1u << 0,
    IRQ_RX_DONE           = 1u << 1,
    IRQ_PREAMBLE_DETECTED = 1u << 2,
    IRQ_SYNC_WORD_VALID   = 1u << 3,
    IRQ_HEADER_VALID      = 1u << 4,
    IRQ_HEADER_ERR        = 1u << 5,
    IRQ_CRC_ERR           = 1u << 6,
    IRQ_CAD_DONE          = 1u << 7,
    IRQ_CAD_DETECTED      = 1u << 8,
};

/* SetCadParams' exit mode: back to standby, or straight into RX when
 * something was found. */
enum { CAD_ONLY = 0x00, CAD_RX = 0x01 };

/* How many symbols into a frame a receiver that has heard it from the start
 * finds the preamble and raises PreambleDetected. A real modem finds it within
 * a few symbols, long before the sync word that `t_pre` marks. Firmware that
 * senses the channel by asking the demodulator measured a blind window of about
 * 4 ms at SF7 and 125 kHz: three boards, 150 trials. So 4 symbols is an upper
 * bound. Raised at the sync word instead, the same firmware is blind for the
 * whole preamble: 29 ms at SF7 with 24 symbols, 46 ms at SF8 with 18. */
constexpr double kPreambleFoundSymbols = 4.0;

enum {
    REG_VERSION_STRING  = 0x0320,
    REG_IQ_CONFIG       = 0x0736,
    REG_SYNC_WORD_MSB   = 0x0740,
    REG_SYNC_WORD_LSB   = 0x0741,
    REG_RANDOM_NUMBER   = 0x0819,   /* four bytes, RandomNumberGen[0..3] */
    REG_SENSITIVITY     = 0x0889,
    REG_RX_GAIN         = 0x08AC,
    REG_TX_CLAMP        = 0x08D8,
    REG_OCP             = 0x08E7,
    REG_RTC_CTRL        = 0x0902,
    REG_EVENT_MASK      = 0x0944,
};

/* The status byte's mode field, and the one command-status value that is
 * neither an error nor silence. */
enum {
    ST_STDBY_RC   = 0x20,
    ST_STDBY_XOSC = 0x30,
    ST_FS         = 0x40,
    ST_RX         = 0x50,
    ST_TX         = 0x60,
    ST_DATA_AVAIL = 0x04,
    ST_CMD_INVALID = 0x08,
};

static const struct simradio_services* S() { return conductor::modelServices(); }

/* The LoRa bandwidth register codes, in hertz. */
static uint32_t bwFromCode(uint8_t code)
{
    switch (code) {
        case 0x00: return 7810;
        case 0x08: return 10420;
        case 0x01: return 15630;
        case 0x09: return 20830;
        case 0x02: return 31250;
        case 0x0A: return 41670;
        case 0x03: return 62500;
        case 0x04: return 125000;
        case 0x05: return 250000;
        case 0x06: return 500000;
        default:   return 125000;
    }
}

/* What the SX1262 radiates for a PA configuration and a SetTxParams power.
 * The power register alone does not say: the chip reaches +14 dBm with the
 * register at 20 and the PA throttled (paDutyCycle 1, hpMax 4), and RadioLib's
 * setOutputPower picks exactly such throttled settings for every power below
 * +22. The table is RadioLib's paOptTable, measured on hardware
 * (jgromes/RadioLib#1628): entry i radiates i − 9 dBm. A triple the table does
 * not name falls back to the datasheet's reference points — full PA radiates
 * the register value, and the three throttled settings the datasheet lists for
 * +20/+17/+14 sit 2/5/8 dB under it. */
struct PaSetting { uint8_t dutyCycle, hpMax; int8_t paVal; };

static const PaSetting kPaMeasured[32] = {
    {2, 2, -5}, {2, 1, 0},  {1, 1, 3},  {1, 2, 0},  {1, 1, 6},  {1, 2, 3},
    {2, 2, 2},  {4, 1, 6},  {1, 1, 11}, {2, 1, 11}, {1, 1, 14}, {2, 1, 14},
    {1, 1, 20}, {1, 1, 22}, {2, 2, 11}, {3, 1, 21}, {1, 2, 17}, {4, 2, 13},
    {1, 2, 20}, {1, 2, 22}, {2, 2, 21}, {3, 2, 21}, {1, 4, 19}, {1, 4, 20},
    {3, 3, 20}, {2, 5, 19}, {1, 6, 22}, {2, 5, 22}, {3, 5, 22}, {3, 6, 22},
    {4, 6, 22}, {4, 7, 22},
};

static int radiatedDbm(uint8_t dutyCycle, uint8_t hpMax, int8_t paVal)
{
    for (int i = 0; i < 32; i++) {
        const PaSetting& s = kPaMeasured[i];
        if (s.dutyCycle == dutyCycle && s.hpMax == hpMax && s.paVal == paVal)
            return i - 9;
    }
    int offset = 0;
    if      (dutyCycle == 3 && hpMax == 5) offset = -2;
    else if (dutyCycle == 2 && hpMax == 3) offset = -5;
    else if (dutyCycle == 2 && hpMax == 2) offset = -8;
    int dbm = paVal + offset;
    return dbm < -9 ? -9 : dbm > 22 ? 22 : dbm;
}

/* The front end between the chip and the connector, from SIM_MESH_BOARD (the
 * firmware contract): what the medium hears is the connector's power, and what
 * the chip reads is the connector's level plus the LNA's gain. The transmit
 * side is the board's curve (fem_tx_cal: `<part> <grade> <reg>:<ant>,…`,
 * entries separated by `;`, the one named fem_part taken), straight lines
 * between its points and flat outside them, rounded, so a firmware that
 * converts with that curve radiates what it asked for. With no curve it is the flat
 * fem_gain_db, and with no front end at all, identity. One front end serves
 * every slot of the station: a board with two radios behind two front ends is
 * not described by SIM_MESH_BOARD. */
struct FrontEnd {
    int n = 0;
    int chip[16] = {}, ant[16] = {};
    int gainDb = 0;
    int rxGainDb = 0;
};

static FrontEnd loadFrontEnd()
{
    FrontEnd fe;
    const char* env = getenv("SIM_MESH_BOARD");
    simradio_json::Object board;
    if (!env || !board.parse(env, strlen(env))) return fe;
    fe.gainDb = (int)board.num("fem_gain_db", 0);
    fe.rxGainDb = (int)board.num("fem_rx_gain_db", 0);
    const std::string part = board.str("fem_part", "");
    const std::string spec = board.str("fem_tx_cal", "");
    if (part.empty()) return fe;

    size_t at = 0;
    while (at < spec.size()) {
        size_t end = spec.find(';', at);
        if (end == std::string::npos) end = spec.size();
        char name[32] = "", grade[32] = "";
        int used = 0;
        std::string entry = spec.substr(at, end - at);
        at = end + 1;
        if (sscanf(entry.c_str(), " %31s %31s %n", name, grade, &used) < 2) continue;
        if (part != name) continue;
        FrontEnd curve = fe;
        const char* p = entry.c_str() + used;
        int reg, dbm, adv;
        while (curve.n < 16 && sscanf(p, " %d : %d %n", &reg, &dbm, &adv) == 2) {
            if (curve.n && reg <= curve.chip[curve.n - 1]) { curve.n = 0; break; }
            curve.chip[curve.n] = reg;
            curve.ant[curve.n] = dbm;
            curve.n++;
            p += adv;
            if (*p != ',') break;
            p++;
        }
        if (curve.n >= 2) return curve;
        fprintf(stderr, "simradio: SIM_MESH_BOARD fem_tx_cal entry %s is not a curve; "
                        "flat %d dB taken\n", name, fe.gainDb);
        return fe;
    }
    return fe;
}

static const FrontEnd& frontEnd()
{
    static const FrontEnd fe = loadFrontEnd();
    return fe;
}

static int connectorDbm(int chipDbm)
{
    const FrontEnd& fe = frontEnd();
    if (fe.n < 2) return chipDbm + fe.gainDb;
    if (chipDbm <= fe.chip[0]) return fe.ant[0];
    if (chipDbm >= fe.chip[fe.n - 1]) return fe.ant[fe.n - 1];
    for (int i = 1; i < fe.n; i++) {
        if (chipDbm > fe.chip[i]) continue;
        const int x0 = fe.chip[i - 1], x1 = fe.chip[i];
        const int y0 = fe.ant[i - 1], y1 = fe.ant[i];
        const int num = (y1 - y0) * (chipDbm - x0);
        const int den = x1 - x0;
        return y0 + (num >= 0 ? (num + den / 2) / den : -((-num + den / 2) / den));
    }
    return fe.ant[fe.n - 1];
}

/* BUSY as the board's chip spends it, from SIM_MESH_BOARD (busy.h, which the
 * LR2021's model reads too): `busy_us` names the commands that keep BUSY high
 * and for how long, `<command>:<µs>` entries separated by commas, each command
 * named as the datasheet names it, bare or as `sx1262.<command>` (an entry
 * qualified with another chip's name is that chip's, and nothing here); and
 * `busy_tcxo` 1 holds BUSY through the TCXO's start-up (`oscReadyUs`, the
 * delay the firmware programmed with SetDIO3AsTCXOCtrl) after every command
 * that needs the oscillator, SetTx included, its own figure counted from the
 * start-up's end. A driver waits BUSY out as on a board. While BUSY is high
 * after a command that changes the radio's mode or tuning, or calibrates it,
 * the radio is deaf: the state it sends says when it is ready again
 * (`ready_at`, never before the reference has started), and the ether tells
 * it of nothing that starts before then but energy. Unset, BUSY is never
 * busy, as before.
 *
 * Measured on a RAK board's SX1262, BUSY after each command in µs over 200
 * switches of channel and spreading factor. A driver that goes to STDBY_RC and
 * calibrates the image at every configure: CalibrateImage 6632, SetRx 5140
 * (some 5075 of it the TCXO's start after STDBY_RC, for the 5 ms the firmware
 * programs), SetModulationParams 92, SetRfFrequency 91, SetStandby 68,
 * SetPacketParams 33, SetTxParams 1: 12.36 ms a switch, 12.10 ms of it BUSY.
 * One that stays in STDBY_XOSC and calibrates only when the band changes:
 * SetRfFrequency 69, SetStandby 69, SetRx 65, SetModulationParams 61,
 * SetPacketParams 33, SetTxParams 1: 0.55 ms a switch, 0.34 ms of it BUSY.
 * (The LR2021's figures are beside its own model, model_lr2021.cpp.)
 * SetTx is not charged: its BUSY would hold the frame back, and the frame
 * goes on the air at the command.
 *
 * `host_us` holds BUSY that much longer after every transaction, reads
 * included: the board's own time for one, its SPI and its driver around it,
 * which a station computing in no time does not spend, and which a driver
 * that waits BUSY out before each transaction then pays as the board's did.
 * The switches above less their BUSY, 0.26 ms over the stock driver's nine
 * transactions and 0.21 ms over the other's eight, give some 29 µs. */
struct BusyFigures {
    int32_t us[256] = {};       /* by opcode */
    bool    tcxo = false;
    int32_t host = 0;           /* every transaction's */
};

static const struct { const char* name; uint8_t op; } kBusyCommands[] = {
    {"SetStandby", CMD_SET_STANDBY},         {"SetSleep", CMD_SET_SLEEP},
    {"SetFs", CMD_SET_FS},                   {"SetRx", CMD_SET_RX},
    {"SetCad", CMD_SET_CAD},                 {"Calibrate", CMD_CALIBRATE},
    {"CalibrateImage", CMD_CALIBRATE_IMAGE}, {"SetRfFrequency", CMD_SET_RF_FREQUENCY},
    {"SetPacketType", CMD_SET_PACKET_TYPE},  {"SetModulationParams", CMD_SET_MODULATION},
    {"SetPacketParams", CMD_SET_PACKET_PARAMS}, {"SetCadParams", CMD_SET_CAD_PARAMS},
    {"SetTxParams", CMD_SET_TX_PARAMS},      {"SetPaConfig", CMD_SET_PA_CONFIG},
    {"SetBufferBaseAddress", CMD_SET_BUFFER_BASE}, {"SetDioIrqParams", CMD_SET_DIO_IRQ_PARAMS},
    {"ClearIrqStatus", CMD_CLEAR_IRQ_STATUS}, {"SetRxTxFallbackMode", CMD_SET_RXTX_FALLBACK},
    {"SetRegulatorMode", CMD_SET_REGULATOR}, {"SetDIO3AsTCXOCtrl", CMD_SET_DIO3_TCXO},
    {"SetDIO2AsRfSwitchCtrl", CMD_SET_DIO2_RF_SWITCH}, {"WriteRegister", CMD_WRITE_REGISTER},
    {"WriteBuffer", CMD_WRITE_BUFFER},
};

static BusyFigures loadBusy()
{
    BusyFigures b;
    const BoardBusy board = readBoardBusy("sx1262");
    b.tcxo = board.tcxo;
    b.host = board.hostUs;
    for (const BusyEntry& e : board.entries) {
        bool known = false;
        for (const auto& c : kBusyCommands) {
            if (e.command != c.name) continue;
            b.us[c.op] = e.us;
            known = true;
        }
        if (!known)
            fprintf(stderr, "simradio: SIM_MESH_BOARD busy_us entry \"%s:%d\" is not "
                            "an SX1262 command BUSY is charged to; ignored\n",
                    e.command.c_str(), (int)e.us);
    }
    return b;
}

static const BusyFigures& busyFigures()
{
    static const BusyFigures b = loadBusy();
    return b;
}

/* SNR as the packet status reports it: x/4 dB in a signed byte. Cast straight
 * into that byte, a level past either end wraps round, so a link 54 dB over the
 * noise would read -10 dB and the strongest links would pass for the weakest,
 * to anything that weighs a link by its SNR. A LoRa receiver's estimate stops
 * well short of the byte's +31.75 dB anyway: it saturates a little above 10 dB
 * however strong the link (an LR2021 on a desk read 14 dB at -16 dBm). So a
 * link reads no more than +12 dB, and no less than the byte's -32 dB. */
/* RandomNumberGen while the chip receives: the chip samples its receiver's
 * noise, which differs from chip to chip. Here it is the C library's
 * getrandom, which the time shim keys by the run's seed and the station in a
 * virtual-time run, so each station draws its own and a run repeats. */
static uint8_t randomByte()
{
    uint8_t b = 0;
    while (getrandom(&b, 1, 0) != 1) { }
    return b;
}

static const int kSnrCeilingDb = 12;

static uint8_t snrRegister(int db)
{
    const int v = 4 * (db > kSnrCeilingDb ? kSnrCeilingDb : db);
    return (uint8_t)(int8_t)(v < -128 ? -128 : v);
}

/* A connector level as the chip reads it, in its -x/2 byte's range. The LNA
 * raises signal and noise alike, so SNR is left as the ether gave it. */
static int chipLevelDbm(int connectorLevel)
{
    const int dbm = connectorLevel + frontEnd().rxGainDb;
    return dbm < -127 ? -127 : dbm > 0 ? 0 : dbm;
}

/* One frame on the air at this antenna, as the ether has told of it: its
 * number, its level and when it leaves the air. */
struct AirFrame {
    int     id = 0;
    int     levelDbm = 0;
    int64_t endUs = 0;
};

/* How many frames at once the instantaneous RSSI keeps track of. More than a
 * channel carries at once in practice; past it, the frame that leaves the air
 * first makes room. */
constexpr int kAirFrames = 16;

/* The chip's state: everything a power cycle puts back. */
struct ChipState {
    const char* mode = "STDBY_RC";
    uint8_t     modeBits = ST_STDBY_RC;
    uint8_t     fallbackBits = ST_STDBY_RC;
    const char* fallbackMode = "STDBY_RC";

    /* The reference oscillator. With DIO3 driving a TCXO (SetDIO3AsTCXOCtrl)
     * the part powers it down in STDBY_RC and SLEEP, and leaving those for a
     * mode that runs on it waits out the start-up the driver programmed
     * before anything happens: the carrier, the receiver, the CAD (BUSY high
     * on the part). No TCXO control (a crystal) is no wait, as before. */
    int64_t tcxoUs = 0;
    int64_t oscReadyUs = 0;    /* when the reference running now is, or was, ready; 0 stopped */

    uint16_t irqStatus = 0;
    uint16_t irqMask = 0;
    uint16_t dio1Mask = 0;

    std::map<uint16_t, uint8_t> regs;
    uint8_t  buf[256] = {};
    uint8_t  txBase = 0, rxBase = 0;

    uint32_t freqHz = 869525000;
    uint32_t bwHz = 125000;
    int      sf = 8;
    int      cr = 5;
    int      preamble = 8;
    bool     hdrImplicit = false;
    bool     crcOn = true;
    bool     iqInverted = false;
    uint8_t  payloadLen = 0;
    int      paVal = 14;
    uint8_t  paDutyCycle = 4;
    uint8_t  paHpMax = 7;

    uint8_t  rxLen = 0, rxPtr = 0;
    uint8_t  rssiPkt = 220, snrPkt = 40, sigRssiPkt = 220;

    /* The air, and the one frame being demodulated out of it. A receiver
     * follows a single frame at a time: the rest is energy, which is what an
     * instantaneous RSSI reads and what carrier sense acts on. The reading
     * is the power of everything in flight summed, as the ether's own busy
     * test sums it, not the strongest part of it: two frames at -80 dBm read
     * -77. Each frame counts once, however often the ether tells of it. */
    AirFrame air[kAirFrames];
    int      lockId = 0;             /* the frame this receiver is following */
    bool     hdrOk = true;           /* whether that frame's header will decode */

    /* When the last frame this antenna has been told of leaves the air. Not
     * the chip's state but the air's, so no mode change clears it: a driver
     * goes RX → standby → CAD, and the frame it was hearing is still in the
     * air when the CAD looks. The medium tells a station about a frame only
     * while it listens, so this holds the frames that began while it did. */
    int64_t  heardEndUs = 0;

    /* Channel activity detection, as SetCadParams left it. */
    int      cadSymbols = 2;
    uint8_t  cadDetPeak = 0, cadDetMin = 0;
    uint8_t  cadExitMode = CAD_ONLY;

    int      txId = 0;

    /* BUSY (`busyFigures()`): when the last command's falls. */
    int64_t  busyUntil = 0;

    VirtualRxEnd pendingEnd = {};
    uint8_t      pendingPayload[256] = {};
    size_t       pendingLen = 0;
    bool         pendingValid = false;

    ChipState()
    {
        /* The datasheet reset values the drivers read-modify-write. Anything
         * a driver writes and reads back lands in the map on the way past;
         * these are the ones it reads before it has written them. */
        regs[REG_SENSITIVITY] = 0x94;
        regs[REG_TX_CLAMP]    = 0x18;
        regs[REG_IQ_CONFIG]   = 0x0D;
        regs[REG_RTC_CTRL]    = 0x00;
        regs[REG_EVENT_MASK]  = 0x00;
        regs[REG_RX_GAIN]     = 0x94;
        regs[REG_OCP]         = 0x38;
        regs[REG_SYNC_WORD_MSB] = 0x14;
        regs[REG_SYNC_WORD_LSB] = 0x24;

        /* The part answers for itself at the version register. An SX1262
         * reports "SX1261" there — the two are the same silicon — and that is
         * the string drivers match on. */
        static const char kVersion[16] = { 'S','X','1','2','6','1',' ','V','2','D',' ','2','D','0','2', 0 };
        for (int i = 0; i < 16; i++)
            regs[(uint16_t)(REG_VERSION_STRING + i)] = (uint8_t)kVersion[i];
    }

    uint8_t status() const { return (uint8_t)(modeBits | ST_DATA_AVAIL); }
};

struct simradio {
    int  slot = 0;
    void (*onPin)(void*, int, int) = nullptr;
    void* ctx = nullptr;

    ChipState st;

    /* Every scheduled instant of a frame in flight, in or out. Created on
     * first use and kept for the chip's life. */
    void* tTxDone = nullptr;
    int64_t txEnd = 0;          /* when tTxDone lands, on the model's clock */
    void* tPre = nullptr;
    void* tSync = nullptr;
    void* tHdr = nullptr;
    void* tCad = nullptr;

    /* The end of a TCXO start-up, and the frame SetTx asked for before it:
     * it goes on the air when the reference is ready. */
    void* tOscReady = nullptr;
    bool pendTxValid = false;
    EtherTxFrame pendTx = {};
    uint8_t pendTxPayload[256] = {};
    void* tBusy = nullptr;
};

namespace {

simradio* s_chips[kMaxSlots] = {};

/* Who to tell when DIO1 moves, taken under the lock and called outside it:
 * at once, or, while a datagram from the ether is applied, when it is in. */
struct PinCall {
    void (*fn)(void*, int, int) = nullptr;
    void* ctx = nullptr;
    int   pin = SIMRADIO_PIN_DIO1;
    bool  high = false;

    void operator()() const;
    void tell() const { if (fn) fn(ctx, pin, high ? 1 : 0); }
};

std::atomic<bool>    s_pinsHeld{false};
std::mutex           s_pinsMu;
std::vector<PinCall> s_pinsKept;

void PinCall::operator()() const
{
    if (!fn) return;
    if (s_pinsHeld.load()) {
        std::lock_guard<std::mutex> g(s_pinsMu);
        s_pinsKept.push_back(*this);
        return;
    }
    tell();
}

PinCall dio1Of(const simradio* c)
{
    PinCall p;
    p.fn = c->onPin;
    p.ctx = c->ctx;
    p.high = (c->st.irqStatus & c->st.dio1Mask) != 0;
    return p;
}

PinCall busyOf(const simradio* c)
{
    PinCall p;
    p.fn = c->onPin;
    p.ctx = c->ctx;
    p.pin = SIMRADIO_PIN_BUSY;
    p.high = S()->now_us() < c->st.busyUntil;
    return p;
}

/* The sync word as the ether matches on it: the two nibble-expanded register
 * bytes read back as the one 8-bit word the driver set. */
uint8_t syncWordOf(const ChipState& d)
{
    auto msb = d.regs.find(REG_SYNC_WORD_MSB);
    auto lsb = d.regs.find(REG_SYNC_WORD_LSB);
    uint8_t m = msb == d.regs.end() ? 0x14 : msb->second;
    uint8_t l = lsb == d.regs.end() ? 0x24 : lsb->second;
    return (uint8_t)((m & 0xF0) | ((l & 0xF0) >> 4));
}

/* Whether a mode runs on the reference oscillator: all but STDBY_RC and SLEEP. */
bool oscillates(const char* mode)
{
    return strcmp(mode, "STDBY_RC") != 0 && strcmp(mode, "SLEEP") != 0;
}

/* What is left of a TCXO start-up, from now. */
int64_t oscWaitUs(const ChipState& d, int64_t now)
{
    return d.oscReadyUs > now ? d.oscReadyUs - now : 0;
}

/* What command `op` (its first parameter `arg`) keeps BUSY high for, from now,
 * once it has set its mode: its own figure, after what is left of the TCXO's
 * start-up when it needs the oscillator and the board says so (`busy_tcxo`),
 * and the host's time for the transaction. SetTx's own is not charged (see
 * `busyFigures`). */
int64_t busyFor(const ChipState& d, uint8_t op, uint8_t arg)
{
    const BusyFigures& b = busyFigures();
    int64_t us = op == CMD_SET_TX ? 0 : b.us[op];
    const bool needs = op == CMD_SET_RX || op == CMD_SET_TX || op == CMD_SET_FS ||
                       op == CMD_SET_CAD || (op == CMD_SET_STANDBY && arg == 0x01);
    if (needs && b.tcxo) us += oscWaitUs(d, S()->now_us());
    return us + b.host;
}

/* SIM_MESH_MULTI_SF: the station's radio has an LR2021's side detectors. Set
 * (and not "0"), its receiver hears the faster spreading factors below its
 * own on the same bandwidth, as that chip's multi-SF receive does: the same
 * air, one demodulator, the frame at whichever SF it came. An SX1262 has no
 * such thing, so it is off unless a station asks; it lets a firmware built
 * on the SX1262's driver be judged against the LR2021's receiver
 * (libsimradio-lr2021.so is that chip itself). */
static bool multiSfReceiver()
{
    static const bool on = [] {
        const char* v = getenv("SIM_MESH_MULTI_SF");
        return v && *v && strcmp(v, "0") != 0;
    }();
    return on;
}

/* The spreading factors an LR2021 listens for at `sf` and `bwHz` (its
 * datasheet, §9.3 and §9.9.6): `sf` and the faster ones below it, down to
 * SF5, as many as the chip allows. Four at most, all within 4 of each other;
 * two side detectors above 500 kHz; the sum of their detection factors times
 * the bandwidth under 32e6. In receive the smallest is on the main detector
 * ("the main SF ... must be the smallest SF in normal Rx operation"), so the
 * rule "one side detector where the main SF is 10 to 12" is on the set's
 * lowest, `lo`: SF10 at 125 kHz hears SF7 to SF10, SF7 the main one. The
 * faster ones go first where something has to give, so `sf` is always among
 * them. Ascending; returns the count. */
static int multiSfSet(int sf, uint32_t bwHz, int out[4])
{
    static const uint64_t kDetectionFactor[8] = {10, 10, 12, 12, 14, 14, 16, 16};
    if (sf < 5 || sf > 12) {
        out[0] = sf;
        return 1;
    }
    int lo = sf - 3 < 5 ? 5 : sf - 3;
    for (; lo < sf; lo++) {
        int sides = sf - lo;
        uint64_t factors = 0;
        for (int s = lo; s <= sf; s++) factors += kDetectionFactor[s - 5];
        if ((bwHz <= 500000 || sides <= 2) && (lo < 10 || sides <= 1)
                && factors * (uint64_t)bwHz < 32000000ULL)
            break;
    }
    int n = 0;
    for (int s = lo; s <= sf; s++) out[n++] = s;
    return n;
}

/* `multiSfSet`'s set, minus `d.sf` itself (the main detector's), as
 * EtherState side detectors (see ether_link.h): the main one's sync word
 * and IQ, since this sweep never models those separately per detector. A
 * thin translation at the edge onto the wire shape `detector()` (ether.py)
 * already matches multi-detector radios on; model_lr2021.cpp's own side
 * detectors (`d.sideSf`) use the same shape directly. */
static void fillMultiSfSide(const ChipState& d, EtherState& s)
{
    s.nSide = 0;
    if (!multiSfReceiver())
        return;
    int all[4], n = multiSfSet(d.sf, d.bwHz, all);
    for (int i = 0; i < n && s.nSide < kMaxSideDetectors; i++) {
        if (all[i] == d.sf) continue;
        s.side[s.nSide].sf = all[i];
        s.side[s.nSide].syncWord = syncWordOf(d);
        s.side[s.nSide].iqInverted = d.iqInverted;
        s.nSide++;
    }
}

void fillState(const simradio* c, EtherState& s)
{
    const ChipState& d = c->st;
    s.slot        = c->slot;
    int64_t now   = S()->now_us();
    bool starting = oscWaitUs(d, now) > 0;
    s.mode        = starting ? "FS" : d.mode;   /* the reference still starting */
    /* Ready once BUSY has fallen (`busyFigures`) and the reference started. */
    s.readyAt     = std::max({now, d.busyUntil, d.oscReadyUs});
    s.freqHz      = d.freqHz;
    s.bwHz        = d.bwHz;
    s.sf          = d.sf;
    s.cr          = d.cr;
    s.syncWord    = syncWordOf(d);
    s.hdrImplicit = d.hdrImplicit;
    s.crc         = d.crcOn;
    s.preamble    = d.preamble;
    s.iqInverted  = d.iqInverted;      /* an SX1262 has no side detectors on its own */
    fillMultiSfSide(d, s);
}

void setMode(ChipState& d, const char* mode, uint8_t bits)
{
    /* A mode without the reference stops it, and a start-up under way with it. */
    if (!oscillates(mode))
        d.oscReadyUs = 0;
    else if (!oscillates(d.mode))
        d.oscReadyUs = S()->now_us() + d.tcxoUs;
    d.mode = mode;
    d.modeBits = bits;
}

/* Arm a one-shot on this chip, making the timer the first time. Starting a
 * running timer restarts it — the services promise that. */
void armOnce(simradio* c, void** h, void (*cb)(void*), int64_t delayUs)
{
    if (!*h) *h = S()->timer_create(cb, c, "sx126x");
    if (!*h) return;
    S()->timer_start_once(*h, delayUs < 0 ? 0 : delayUs);
}

void stopTimer(void* h)
{
    if (h) S()->timer_stop(h);
}

/* The demodulator lets go of the frame it was following. */
void dropLock(simradio* c)
{
    ChipState& d = c->st;
    d.lockId = 0;
    d.pendingValid = false;
    stopTimer(c->tPre);
    stopTimer(c->tSync);
    stopTimer(c->tHdr);
}

/* Leaving RX for anything but CAD abandons whatever was arriving, the energy
 * with it. CAD keeps the energy: a frame the receiver was following is still
 * on the air, and it is what a CAD is for. */
void abandonReception(simradio* c)
{
    for (AirFrame& a : c->st.air) a = AirFrame();
    dropLock(c);
}

/* A frame's energy arriving at this antenna: kept until it leaves the air,
 * once per frame number. */
void feelAir(ChipState& d, int id, int levelDbm, int64_t endUs, int64_t now)
{
    int room = -1, soonest = 0;
    for (int i = 0; i < kAirFrames; i++) {
        AirFrame& a = d.air[i];
        if (a.endUs > now && a.id == id) {          /* told again: one frame */
            a.levelDbm = levelDbm;
            a.endUs = endUs;
            return;
        }
        if (a.endUs <= now && room < 0) room = i;
        if (a.endUs < d.air[soonest].endUs) soonest = i;
    }
    AirFrame& a = d.air[room >= 0 ? room : soonest];
    a.id = id;
    a.levelDbm = levelDbm;
    a.endUs = endUs;
}

/* What the instantaneous RSSI reads at the connector: the frames in flight,
 * their powers summed, or the floor when there are none. */
int airLevelDbm(const ChipState& d, int64_t now)
{
    double mw = 0.0;
    for (const AirFrame& a : d.air)
        if (a.endUs > now) mw += std::pow(10.0, a.levelDbm / 10.0);
    return mw > 0.0 ? (int)std::lround(10.0 * std::log10(mw)) : kNoiseFloorDbm;
}

void txDoneCb(void* arg);
void oscReadyCb(void* arg);
void rxPreCb(void* arg);
void rxSyncCb(void* arg);
void rxHdrCb(void* arg);
void rxEndCb(void* arg);
void cadDoneCb(void* arg);
void busyDoneCb(void* arg);

}  // namespace

/* ---- The registry ---- */

simradio* modelChip(int slot)
{
    if (slot < 0 || slot >= kMaxSlots) return nullptr;
    S()->lock();
    simradio* c = s_chips[slot];
    S()->unlock();
    return c;
}

void modelHoldPins()
{
    s_pinsHeld.store(true);
}

void modelReleasePins()
{
    std::vector<PinCall> kept;
    {
        std::lock_guard<std::mutex> g(s_pinsMu);
        kept.swap(s_pinsKept);
        s_pinsHeld.store(false);
    }
    for (const PinCall& p : kept) p.tell();
}

extern "C" simradio_t* simradio_open(int slot, void (*on_pin)(void*, int, int), void* ctx)
{
    if (slot < 0 || slot >= kMaxSlots) return nullptr;
    S()->lock();
    simradio* c = s_chips[slot];
    if (!c) {
        c = new simradio();
        c->slot = slot;
        s_chips[slot] = c;
    } else {
        /* A chip opened again is a chip powered up again. The object itself
         * lives for the process, because a timer may be about to fire on it. */
        stopTimer(c->tTxDone);
        stopTimer(c->tPre);
        stopTimer(c->tSync);
        stopTimer(c->tHdr);
        stopTimer(c->tCad);
        stopTimer(c->tOscReady);
        c->pendTxValid = false;
        stopTimer(c->tBusy);
        c->st = ChipState();
    }
    c->onPin = on_pin;
    c->ctx = ctx;
    S()->unlock();
    return c;
}

extern "C" void simradio_close(simradio_t* c)
{
    if (!c) return;
    S()->lock();
    stopTimer(c->tTxDone);
    stopTimer(c->tPre);
    stopTimer(c->tSync);
    stopTimer(c->tHdr);
    stopTimer(c->tCad);
    stopTimer(c->tOscReady);
    c->pendTxValid = false;
    stopTimer(c->tBusy);
    c->onPin = nullptr;
    c->ctx = nullptr;
    S()->unlock();
}

extern "C" int64_t simradio_now_us(void)
{
    return S()->now_us();
}

/* A transmitting chip changes only when TX_DONE lands: the ether's frames are
 * not taken in outside RX and CAD (modelRxBegin, modelRxEnd), and nothing but
 * a command ends TX early, which the polling driver would be the one to send.
 * TX_DONE is seen at the first node time whose T has reached the frame's end. */
extern "C" void simradio_host_floor(int station)
{
    etherPublishFloor(station != 0);
}

extern "C" int64_t simradio_quiet_for_us(simradio_t* c)
{
    if (!c || !conductor::isVirtual()) return -1;
    S()->lock();
    int64_t end = strcmp(c->st.mode, "TX") == 0 ? c->txEnd : -1;
    int64_t now = S()->now_us();
    S()->unlock();
    if (end <= now) return -1;
    int64_t left = conductor::firstNodeAt(end) - conductor::nodeNowUs();
    return left > 0 ? left : -1;
}

/* ---- The lines ---- */

extern "C" int simradio_pin(simradio_t* c, int pin)
{
    if (!c) return 0;
    /* BUSY is high while the last command's time lasts (`busyFigures`), and
     * with none charged never: the model answers a command the moment it is
     * handed one. */
    if (pin != SIMRADIO_PIN_DIO1 && pin != SIMRADIO_PIN_BUSY) return 0;
    S()->lock();
    int high = pin == SIMRADIO_PIN_BUSY ? S()->now_us() < c->st.busyUntil
                                        : (c->st.irqStatus & c->st.dio1Mask) != 0;
    S()->unlock();
    return high;
}

extern "C" void simradio_reset(simradio_t* c)
{
    if (!c) return;
    S()->lock();
    setMode(c->st, "STDBY_RC", ST_STDBY_RC);
    c->st.irqStatus = 0;
    c->st.tcxoUs = 0;
    c->st.oscReadyUs = 0;
    stopTimer(c->tOscReady);
    c->pendTxValid = false;
    PinCall pin = dio1Of(c);
    S()->unlock();
    pin();
}

/* ---- The command interpreter ---- */

extern "C" void simradio_transfer(simradio_t* c, const uint8_t* out, size_t len, uint8_t* in)
{
    if (!c || !out || !in || len == 0) return;

    const uint8_t op = out[0];
    bool     publishState = false;
    bool     publishTx = false;
    EtherTxFrame frame = {};
    EtherState   snap = {};
    uint8_t  txPayload[256];

    S()->lock();
    ChipState& d = c->st;

    /* Every byte of the reply is the status until the data starts. */
    memset(in, d.status(), len);

    switch (op) {
    case CMD_SET_SLEEP:
        setMode(d, "SLEEP", ST_STDBY_RC);
        publishState = true;
        break;

    case CMD_SET_STANDBY:
        if (len >= 2 && out[1] == 0x01) setMode(d, "STDBY_XOSC", ST_STDBY_XOSC);
        else                            setMode(d, "STDBY_RC", ST_STDBY_RC);
        publishState = true;
        break;

    case CMD_SET_FS:
        setMode(d, "FS", ST_FS);
        publishState = true;
        break;

    case CMD_SET_RX:
        setMode(d, "RX", ST_RX);
        publishState = true;
        break;


    case CMD_SET_TX: {
        setMode(d, "TX", ST_TX);
        /* `cr` is already the denominator of 4/n, which is what the formula
         * takes — a frame timed with anything outside 5..8 comes back as no
         * time on the air at all, and then nothing in the medium can ever
         * collide with anything. */
        double toa = loraToaSeconds(d.sf, (int)d.bwHz, d.cr, d.preamble,
                                    d.payloadLen, d.hdrImplicit, d.crcOn);
        double tSym = (double)((uint32_t)1 << d.sf) / (double)d.bwHz;
        int64_t now = S()->now_us();
        fillState(c, frame.state);
        frame.id   = ++d.txId;
        frame.t0   = now;
        frame.tPre = now + (int64_t)((d.preamble + 4.25) * tSym * 1e6);
        frame.tHdr = frame.tPre + (int64_t)(8.0 * tSym * 1e6);
        frame.tEnd = now + (int64_t)(toa * 1e6);
        frame.powerDbm = connectorDbm(radiatedDbm(d.paDutyCycle, d.paHpMax, (int8_t)d.paVal));
        for (int i = 0; i < d.payloadLen; i++) txPayload[i] = d.buf[(uint8_t)(d.txBase + i)];
        frame.payload = txPayload;
        frame.len = d.payloadLen;
        int64_t wait = oscWaitUs(d, now);
        if (wait > 0) {
            /* The carrier comes when the reference is ready (oscReadyCb). */
            c->pendTx = frame;
            memcpy(c->pendTxPayload, txPayload, (size_t)d.payloadLen);
            c->pendTx.payload = c->pendTxPayload;
            c->pendTxValid = true;
            armOnce(c, &c->tOscReady, oscReadyCb, wait);
            publishState = true;
            break;
        }
        publishTx = true;
        c->txEnd = frame.tEnd;
        armOnce(c, &c->tTxDone, txDoneCb, frame.tEnd - now);
        break;
    }

    case CMD_SET_RF_FREQUENCY:
        if (len >= 5) {
            uint32_t frf = ((uint32_t)out[1] << 24) | ((uint32_t)out[2] << 16) |
                           ((uint32_t)out[3] << 8) | out[4];
            d.freqHz = (uint32_t)(((uint64_t)frf * 32000000ULL) >> 25);
            publishState = true;
        }
        break;

    case CMD_SET_MODULATION:
        if (len >= 4) {
            d.sf   = out[1];
            d.bwHz = bwFromCode(out[2]);
            d.cr   = out[3] + 4;          /* the register holds 4/(4+n) */
            if (d.cr < 5) d.cr = 5;
            if (d.cr > 8) d.cr = 8;
            publishState = true;
        }
        break;

    case CMD_SET_PACKET_PARAMS:
        if (len >= 7) {
            d.preamble    = ((int)out[1] << 8) | out[2];
            d.hdrImplicit = out[3] != 0;
            d.payloadLen  = out[4];
            d.crcOn       = out[5] != 0;
            d.iqInverted  = out[6] != 0;
            publishState = true;
        }
        break;

    case CMD_SET_TX_PARAMS:
        if (len >= 2) d.paVal = (int8_t)out[1];
        break;

    case CMD_SET_PA_CONFIG:
        if (len >= 3) {
            d.paDutyCycle = out[1];
            d.paHpMax     = out[2];
        }
        break;

    case CMD_SET_BUFFER_BASE:
        if (len >= 3) { d.txBase = out[1]; d.rxBase = out[2]; }
        break;

    case CMD_WRITE_REGISTER:
        if (len >= 3) {
            uint16_t addr = ((uint16_t)out[1] << 8) | out[2];
            for (size_t i = 3; i < len; i++) d.regs[(uint16_t)(addr + (i - 3))] = out[i];
            publishState = true;   /* the sync word lives here */
        }
        break;

    case CMD_READ_REGISTER:
        if (len >= 4) {
            uint16_t addr = ((uint16_t)out[1] << 8) | out[2];
            bool rx = strcmp(d.mode, "RX") == 0;
            for (size_t i = 4; i < len; i++) {
                uint16_t reg = (uint16_t)(addr + (i - 4));
                if (rx && reg >= REG_RANDOM_NUMBER && reg < REG_RANDOM_NUMBER + 4) {
                    in[i] = randomByte();
                    continue;
                }
                auto it = d.regs.find(reg);
                in[i] = it == d.regs.end() ? 0x00 : it->second;
            }
        }
        break;

    case CMD_WRITE_BUFFER:
        if (len >= 2) {
            uint8_t off = out[1];
            for (size_t i = 2; i < len; i++) d.buf[(uint8_t)(off + (i - 2))] = out[i];
        }
        break;

    case CMD_READ_BUFFER:
        if (len >= 3) {
            uint8_t off = out[1];
            for (size_t i = 3; i < len; i++) in[i] = d.buf[(uint8_t)(off + (i - 3))];
        }
        break;

    case CMD_SET_DIO_IRQ_PARAMS:
        if (len >= 5) {
            d.irqMask  = (uint16_t)(((uint16_t)out[1] << 8) | out[2]);
            d.dio1Mask = (uint16_t)(((uint16_t)out[3] << 8) | out[4]);
        }
        break;

    case CMD_GET_IRQ_STATUS:
        if (len >= 4) {
            in[2] = (uint8_t)(d.irqStatus >> 8);
            in[3] = (uint8_t)(d.irqStatus & 0xFF);
        }
        break;

    case CMD_CLEAR_IRQ_STATUS:
        if (len >= 3) {
            uint16_t clear = (uint16_t)(((uint16_t)out[1] << 8) | out[2]);
            d.irqStatus &= (uint16_t)~clear;
        }
        break;

    case CMD_GET_RX_BUF_STATUS:
        if (len >= 4) { in[2] = d.rxLen; in[3] = d.rxPtr; }
        break;

    case CMD_GET_PACKET_STATUS:
        if (len >= 5) { in[2] = d.rssiPkt; in[3] = d.snrPkt; in[4] = d.sigRssiPkt; }
        break;

    case CMD_GET_RSSI_INST:
        if (len >= 3) {
            /* The chip's -x/2 encoding, and nothing at all outside RX. */
            int dbm = chipLevelDbm(airLevelDbm(d, S()->now_us()));
            in[2] = strcmp(d.mode, "RX") == 0 ? (uint8_t)(-2 * dbm) : 0xFF;
        }
        break;

    case CMD_GET_PACKET_TYPE:
        if (len >= 3) in[2] = 0x01;      /* LoRa */
        break;

    case CMD_GET_STATUS:
        if (len >= 2) in[1] = d.status();
        break;

    case CMD_GET_DEVICE_ERRORS:
        if (len >= 4) { in[2] = 0; in[3] = 0; }
        break;

    case CMD_GET_STATS:
        for (size_t i = 2; i < len; i++) in[i] = 0;
        break;

    /* Accepted and without effect at this depth. */
    case CMD_SET_PACKET_TYPE:
    case CMD_SET_REGULATOR:
    case CMD_CALIBRATE:
    case CMD_CALIBRATE_IMAGE:
    case CMD_SET_DIO2_RF_SWITCH:
    case CMD_STOP_TIMER_ON_PRE:
    case CMD_SET_LORA_SYMB_TO:
    case CMD_CLEAR_DEVICE_ERR:
    case CMD_RESET_STATS:
        break;

    case CMD_SET_DIO3_TCXO:
        /* The voltage, then the start-up as 24 bits of 15.625 µs. */
        if (len >= 5) {
            uint32_t steps = ((uint32_t)out[2] << 16) | ((uint32_t)out[3] << 8) | out[4];
            d.tcxoUs = (int64_t)steps * 1000 / 64;
        }
        break;

    case CMD_SET_CAD_PARAMS:
        if (len >= 5) {
            /* cadSymbolNum 0x00..0x04 is 1, 2, 4, 8, 16 symbols; the timeout
             * (bytes 5..7) only matters to CAD_RX's receive, which ends at
             * the frame here. */
            d.cadSymbols  = 1 << (out[1] > 4 ? 4 : out[1]);
            d.cadDetPeak  = out[2];
            d.cadDetMin   = out[3];
            d.cadExitMode = out[4] == CAD_RX ? CAD_RX : CAD_ONLY;
        }
        break;

    case CMD_SET_CAD: {
        /* The datasheet reports RX in the status byte during CAD; the wire
         * says CAD, which is what the medium delivers energy to. */
        setMode(d, "CAD", ST_RX);
        double tSym = (double)((uint32_t)1 << d.sf) / (double)d.bwHz;
        /* The window opens once the radio is ready: BUSY fallen and the
         * reference started (`fillState`'s `readyAt`). */
        int64_t wait = oscWaitUs(d, S()->now_us());
        armOnce(c, &c->tCad, cadDoneCb,
                std::max(wait, busyFor(d, op, 0)) + (int64_t)(d.cadSymbols * tSym * 1e6));
        if (wait > 0) armOnce(c, &c->tOscReady, oscReadyCb, wait);
        publishState = true;
        break;
    }

    case CMD_SET_RXTX_FALLBACK:
        if (len >= 2) {
            switch (out[1]) {
                case 0x40: d.fallbackMode = "FS";         d.fallbackBits = ST_FS; break;
                case 0x30: d.fallbackMode = "STDBY_XOSC"; d.fallbackBits = ST_STDBY_XOSC; break;
                default:   d.fallbackMode = "STDBY_RC";   d.fallbackBits = ST_STDBY_RC; break;
            }
        }
        break;

    default:
        memset(in, (uint8_t)(d.modeBits | ST_CMD_INVALID), len);
        break;
    }

    if (op == CMD_SET_STANDBY || op == CMD_SET_SLEEP || op == CMD_SET_FS || op == CMD_SET_TX)
        abandonReception(c);
    /* A mode on a reference still starting is said to the medium when it is
     * ready (oscReadyCb); one without the reference stops a start-up, and a
     * frame waiting on it. */
    if (op == CMD_SET_STANDBY || op == CMD_SET_FS || op == CMD_SET_RX || op == CMD_SET_SLEEP) {
        int64_t wait = oscWaitUs(d, S()->now_us());
        if (!oscillates(d.mode)) {
            stopTimer(c->tOscReady);
            c->pendTxValid = false;
        } else if (wait > 0) {
            armOnce(c, &c->tOscReady, oscReadyCb, wait);
        }
    }
    if (op == CMD_SET_CAD)
        dropLock(c);
    /* Any other mode ends a CAD in progress without an answer. */
    if (op == CMD_SET_STANDBY || op == CMD_SET_SLEEP || op == CMD_SET_FS ||
        op == CMD_SET_TX || op == CMD_SET_RX)
        stopTimer(c->tCad);

    /* BUSY for the command's time. After a change of mode or tuning, or a
     * calibration, the radio is deaf until it falls, which the state tells
     * the ether. */
    const int64_t busyUs = busyFor(d, op, len >= 2 ? out[1] : 0);
    PinCall busyPin;
    if (busyUs > 0) {
        d.busyUntil = S()->now_us() + busyUs;
        publishState = publishState || op == CMD_CALIBRATE || op == CMD_CALIBRATE_IMAGE;
        armOnce(c, &c->tBusy, busyDoneCb, busyUs);
        busyPin = busyOf(c);
    }

    if (publishState) fillState(c, snap);
    PinCall pin = dio1Of(c);
    S()->unlock();

    if (publishTx)    etherPublishTx(frame);
    if (publishState) etherPublishState(snap);
    busyPin();
    pin();
}

/* ---- Timer callbacks ---- */

namespace {

void raise(simradio* c, uint16_t bits)
{
    S()->lock();
    c->st.irqStatus |= bits;
    PinCall pin = dio1Of(c);
    S()->unlock();
    pin();
}

/* The reference is ready: a frame SetTx asked for goes on the air now, and
 * otherwise the medium is told the mode the chip has been in since its
 * command. */
void oscReadyCb(void* arg)
{
    auto* c = (simradio*)arg;
    EtherState s;
    EtherTxFrame frame = {};
    bool tx = false;
    S()->lock();
    int64_t now = S()->now_us();
    if (c->pendTxValid && strcmp(c->st.mode, "TX") == 0) {
        frame = c->pendTx;
        int64_t shift = now - frame.t0;
        frame.t0 += shift;
        frame.tPre += shift;
        frame.tHdr += shift;
        frame.tEnd += shift;
        fillState(c, frame.state);
        c->pendTxValid = false;
        c->txEnd = frame.tEnd;
        armOnce(c, &c->tTxDone, txDoneCb, frame.tEnd - now);
        tx = true;
    } else {
        fillState(c, s);
    }
    S()->unlock();
    if (tx) etherPublishTx(frame);
    else    etherPublishState(s);
}

/* TX_DONE lands at the end of the frame, and the chip falls back to whatever
 * SetRxTxFallbackMode named. */
void txDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    EtherState s;
    S()->lock();
    setMode(c->st, c->st.fallbackMode, c->st.fallbackBits);
    fillState(c, s);
    S()->unlock();
    etherPublishState(s);
    raise(c, IRQ_TX_DONE);
}

void rxPreCb(void* arg)
{
    raise((simradio*)arg, IRQ_PREAMBLE_DETECTED);
}

void rxSyncCb(void* arg)
{
    raise((simradio*)arg, IRQ_SYNC_WORD_VALID);
}

/* The header is in: valid, or an error that lets go of the frame. The chip
 * stays in RX and hunts for the next preamble, as continuous RX does. */
void rxHdrCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    bool ok = c->st.hdrOk;
    if (!ok) dropLock(c);
    S()->unlock();
    raise(c, ok ? IRQ_HEADER_VALID : IRQ_HEADER_ERR);
}

void rxEndCb(void* arg)
{
    auto* c = (simradio*)arg;
    uint16_t bits = 0;
    S()->lock();
    ChipState& d = c->st;
    if (d.pendingValid) {
        for (size_t i = 0; i < d.pendingLen; i++)
            d.buf[(uint8_t)(d.rxBase + i)] = d.pendingPayload[i];
        d.rxLen = (uint8_t)d.pendingLen;
        d.rxPtr = d.rxBase;
        d.rssiPkt    = (uint8_t)(-2 * chipLevelDbm(d.pendingEnd.rssiDbm));
        d.sigRssiPkt = d.rssiPkt;
        d.snrPkt     = snrRegister(d.pendingEnd.snrDb);
        bits = IRQ_RX_DONE;
        if (!d.pendingEnd.crcOk) bits |= IRQ_CRC_ERR;
        d.pendingValid = false;
    }
    S()->unlock();
    if (bits) raise(c, bits);
}

/* The CAD window is over: done, and detected if energy is in the air at this
 * antenna now. The chip then goes where SetCadParams said. */
void cadDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    ChipState& d = c->st;
    EtherState s;
    S()->lock();
    if (strcmp(d.mode, "CAD") != 0) { S()->unlock(); return; }   /* ended by a command */
    bool detected = S()->now_us() < d.heardEndUs;
    uint16_t bits = IRQ_CAD_DONE | (detected ? IRQ_CAD_DETECTED : 0);
    if (detected && d.cadExitMode == CAD_RX) setMode(d, "RX", ST_RX);
    else                                     setMode(d, "STDBY_RC", ST_STDBY_RC);
    fillState(c, s);
    S()->unlock();
    etherPublishState(s);
    raise(c, bits);
}

/* BUSY falls when the last command's time is up. */
void busyDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    PinCall pin = busyOf(c);
    S()->unlock();
    pin();
}

}  // namespace

/* ---- What the ether hands back ---- */

void modelRxBegin(simradio* c, const VirtualRxBegin& f)
{
    int64_t now = S()->now_us();
    S()->lock();
    ChipState& d = c->st;
    bool rx = strcmp(d.mode, "RX") == 0;
    bool cad = strcmp(d.mode, "CAD") == 0;
    if (!rx && !cad) { S()->unlock(); return; }

    /* Energy first: every frame in the air raises the instantaneous reading,
     * whether or not this receiver is following it. */
    int64_t endUs = now + (f.tEnd - f.t0);
    feelAir(d, f.id, f.levelDbm, endUs, now);
    if (endUs > d.heardEndUs) d.heardEndUs = endUs;

    /* A CAD senses; it demodulates nothing. Nor does an RX slot follow a frame
     * the ether marks as energy. */
    if (cad || f.energyOnly) { S()->unlock(); return; }

    /* Then the demodulator, which follows one frame at a time: the one the
     * ether last began on it. Whether a later frame takes the receiver off
     * the one in progress is the ether's decision, because only the ether
     * sees everything arriving at this antenna summed; a frame that does not
     * is sent here as energy. An rx_end for any frame but this one is not
     * this receiver's. */
    d.lockId = f.id;
    d.hdrOk = f.hdrOk;

    /* The sender's stamps are its own clock's; only the gaps between them mean
     * anything here, and they are measured from this instant. The preamble is
     * found a few symbols in, and the sync word lands where `t_pre` says. */
    int64_t syncUs = f.tPre - f.t0;
    double tSym = (double)((uint32_t)1 << d.sf) / (double)d.bwHz;
    int64_t foundUs = (int64_t)(kPreambleFoundSymbols * tSym * 1e6);
    armOnce(c, &c->tPre, rxPreCb, foundUs < syncUs ? foundUs : syncUs);
    armOnce(c, &c->tSync, rxSyncCb, syncUs);
    armOnce(c, &c->tHdr, rxHdrCb, f.tHdr - f.t0);
    S()->unlock();
}

void modelRxEnd(simradio* c, const VirtualRxEnd& f)
{
    S()->lock();
    ChipState& d = c->st;
    if (strcmp(d.mode, "RX") != 0 || f.id != d.lockId) { S()->unlock(); return; }
    d.lockId = 0;
    size_t n = f.len > sizeof(d.pendingPayload) ? sizeof(d.pendingPayload) : f.len;
    if (f.payload && n) memcpy(d.pendingPayload, f.payload, n);
    d.pendingLen = n;
    d.pendingEnd = f;
    d.pendingEnd.payload = nullptr;
    d.pendingValid = true;
    S()->unlock();

    rxEndCb(c);
}
