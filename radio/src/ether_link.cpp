/**
 * ether_link — see the header.
 *
 * The socket and the thread that reads it are the backend's (services.h), so
 * this file is the wire and nothing else: what goes out, and what an arriving
 * message does to which chip. In a virtual-time run every message the ether
 * sends carries the instant it happens at and a sequence number: the
 * conductor moves T there first, the message is applied, and the station owes
 * the ether an idle for that number. The link is UDP: messages are applied in
 * their numbers' order and never twice, and a lost one on either side is
 * recovered by an idle said again (conductor::resendIdle).
 */
#include "ether_link.h"

#include "conductor.h"
#include "json.h"
#include "model.h"
#include "services.h"
#include "simradio.h"

#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

namespace {

constexpr size_t kMaxPayload = 256;

/* How long, in wall time, station_open waits for the ether's welcome. */
constexpr int kJoinWaitMs = 60 * 1000;

/* How long, in wall time, a station that has read a host's bytes waits for
 * the ether to bring it to the run's T before it goes on without. */
constexpr int kFloorWaitMs = 2000;

/* Runs the ether sent in answer to a floor to the station, applied. */
std::atomic<uint64_t> s_floorRuns{0};

const struct simradio_services* S() { return conductor::modelServices(); }

int  s_fd = -1;
int  s_sid = 1;
bool s_started = false;

void sendRaw(const char* s, size_t n)
{
    if (s_fd < 0) return;
    ssize_t w = send(s_fd, s, n, MSG_DONTWAIT);
    (void)w;
}

/* Anything but an idle: the ether takes it as this station no longer idle. */
void sendLine(const char* s, size_t n)
{
    sendRaw(s, n);
    conductor::spoke();
}

void sendIdle(uint64_t seq, int64_t until, uint64_t idleN)
{
    char line[192];
    int n;
    if (until == conductor::kNever)
        n = snprintf(line, sizeof line,
                     "{\"type\":\"idle\",\"sid\":%d,\"seq\":%llu,\"until\":null,\"n\":%llu}",
                     s_sid, (unsigned long long)seq, (unsigned long long)idleN);
    else
        n = snprintf(line, sizeof line,
                     "{\"type\":\"idle\",\"sid\":%d,\"seq\":%llu,\"until\":%lld,\"n\":%llu}",
                     s_sid, (unsigned long long)seq, (long long)until, (unsigned long long)idleN);
    sendRaw(line, (size_t)n);
}

void appendState(char* p, size_t cap, size_t* at, const EtherState& s)
{
    *at += (size_t)snprintf(p + *at, cap - *at,
        "\"slot\":%d,\"mod\":\"lora\",\"freq\":%u,\"bw\":%u,\"sf\":%d,\"cr\":%d,\"sync\":%d,"
        "\"hdr\":\"%s\",\"crc\":%s,\"pre\":%d,\"iq\":\"%s\"",
        s.slot, (unsigned)s.freqHz, (unsigned)s.bwHz, s.sf, s.cr, s.syncWord,
        s.hdrImplicit ? "implicit" : "explicit", s.crc ? "true" : "false", s.preamble,
        s.iqInverted ? "inverted" : "normal");
}

/* ---- Inbound ---- */

void handleMessage(const char* text, size_t len)
{
    simradio_json::Object msg;
    if (!msg.parse(text, len)) return;
    std::string type = msg.str("type", "");
    bool timed = conductor::isVirtual() && msg.has("seq");

    if (type == "welcome") {
        /* The clock first: until the welcome is taken nothing in the station
         * can wait on time, the log included. */
        std::string mode = msg.str("mode", "real");
        conductor::welcome(mode == "virtual", msg.num("t", 0), msg.num("epoch", 0),
                           (uint64_t)msg.num("seq", 0));
        S()->log(SIMRADIO_LOG_INFO, "ether: %s time at t %lld", mode.c_str(),
                 (long long)msg.num("t", 0));
        return;
    }

    if (timed) {
        /* Applied strictly in sequence: one said again is already applied,
         * and one past a gap waits for what was lost, which an idle for the
         * last number applied asks the ether for (conductor::resendIdle). */
        uint64_t seq = (uint64_t)msg.num("seq", 0), have = conductor::lastSeq();
        if (seq <= have) return;
        if (seq > have + 1) {
            conductor::resendIdle();
            return;
        }
        conductor::advanceTo(msg.num("t", 0));
    }

    if (type == "rx_begin") {
        simradio* chip = modelChip((int)msg.num("slot", 0));
        if (chip) {
            VirtualRxBegin f = {};
            f.id       = (int)msg.num("id", 0);
            f.t0       = msg.num("t0", 0);
            f.tPre     = msg.num("t_pre", 0);
            f.tHdr     = msg.num("t_hdr", 0);
            f.tEnd     = msg.num("t_end", 0);
            f.levelDbm = (int)msg.num("level", kNoiseFloorDbm);
            f.energyOnly = msg.num("cad", 0) != 0;
            f.hdrOk      = msg.num("hdr_ok", 1) != 0;
            f.det        = (int)msg.num("det", 0);
            modelRxBegin(chip, f);
        }
    } else if (type == "rx_end") {
        simradio* chip = modelChip((int)msg.num("slot", 0));
        if (chip) {
            uint8_t payload[kMaxPayload];
            long plen = 0;
            std::string b = msg.str("payload", "");
            if (!b.empty()) {
                plen = simradio_json::base64Decode(b.data(), b.size(), payload, sizeof payload);
                if (plen < 0) plen = 0;
            }
            std::string verdict = msg.str("verdict", "clean");
            VirtualRxEnd f = {};
            f.id       = (int)msg.num("id", 0);
            f.payload  = payload;
            f.len      = (size_t)plen;
            f.crcOk    = verdict == "clean";
            f.hdrOk    = verdict != "hdr";
            f.rssiDbm  = (int)msg.num("rssi", -80);
            f.snrDb    = (int)msg.num("snr", 10);
            modelRxEnd(chip, f);
        }
    }
    /* `run` is only its instant; anything else is a message this station
     * does not understand. */

    if (timed) conductor::granted((uint64_t)msg.num("seq", 0));
    if (timed && type == "run" && msg.num("floor", 0)) s_floorRuns.fetch_add(1);
}

/* A datagram: one message, or several, one per line, all of one instant (a
 * station that says `lines` in its hello is sent every message of an instant
 * in one). They are applied together: the host is told of nothing (its waits
 * that fall due, DIO1) until all are in, so a thread woken at T finds the
 * whole of T, and not whatever part of it the reader had got to. */
void handleDatagram(const char* text, size_t len)
{
    conductor::hold();
    modelHoldPins();
    const char* p = text;
    const char* end = text + len;
    while (p < end) {
        const char* nl = static_cast<const char*>(memchr(p, '\n', (size_t)(end - p)));
        size_t n = nl ? (size_t)(nl - p) : (size_t)(end - p);
        if (n) handleMessage(p, n);
        if (!nl) break;
        p = nl + 1;
    }
    /* DIO1 first: a host thread the release wakes reads the pin at once. */
    modelReleasePins();
    conductor::release();
}

}  // namespace

/* ---- Outbound ---- */

void etherPublishFloor(bool station)
{
    if (!conductor::isVirtual()) return;
    /* A host can write to the door before the welcome has landed (the door
     * is open before the hello, and the testbed starts talking at the
     * hello): wait for it, or the floor would be lost, and T held for a tool
     * that is waiting on a station that cannot move. Raw sleeps, as below. */
    struct timespec ms = {0, 1000 * 1000};
    for (int i = 0; i < kJoinWaitMs && !conductor::joined(); i++)
        syscall(SYS_nanosleep, &ms, nullptr);
    if (!conductor::joined()) return;
    uint64_t answered = s_floorRuns.load();
    char line[96];
    int n = snprintf(line, sizeof line, "{\"type\":\"floor\",\"sid\":%d,\"to\":\"%s\"}",
                     s_sid, station ? "station" : "tool");
    sendLine(line, (size_t)n);
    if (!station) return;
    /* A station that has been idle has the T it was last given, which may
     * be long past: the run went on without it. What the host wrote is to be
     * taken at the run's T, so the ether answers a floor to the station with
     * a run at it (marked `floor`, so a run already on its way for another
     * reason does not pass for it), and the host's bytes wait for that. Raw
     * sleeps: the time shim's would wait on T. */
    struct timespec step = {0, 50 * 1000};
    for (int i = 0; i < kFloorWaitMs * 20 && s_floorRuns.load() == answered; i++)
        syscall(SYS_nanosleep, &step, nullptr);
    if (s_floorRuns.load() == answered)
        S()->log(SIMRADIO_LOG_WARN, "ether: no run after a host's bytes in %d ms; going on",
                 kFloorWaitMs);
}

void etherPublishState(const EtherState& s)
{
    if (s_fd < 0) return;
    char line[512];
    size_t at = (size_t)snprintf(line, sizeof line,
        "{\"type\":\"state\",\"sid\":%d,\"t\":%lld,\"mode\":\"%s\",\"ready_at\":%lld,",
        s_sid, (long long)S()->now_us(), s.mode, (long long)s.readyAt);
    appendState(line, sizeof line, &at, s);
    if (s.nSide > 0) {
        at += (size_t)snprintf(line + at, sizeof line - at, ",\"side\":[");
        for (int i = 0; i < s.nSide && i < kMaxSideDetectors; i++)
            at += (size_t)snprintf(line + at, sizeof line - at,
                "%s{\"sf\":%d,\"sync\":%d,\"iq\":\"%s\"}", i ? "," : "",
                s.side[i].sf, s.side[i].syncWord,
                s.side[i].iqInverted ? "inverted" : "normal");
        at += (size_t)snprintf(line + at, sizeof line - at, "]");
        /* `sfs`: the main sf and the side detectors', merged and ascending --
         * what a multi-SF rule needs without walking `side` itself.
         * fillState() already dedups `side` against `sf` and each other, so
         * a plain insertion sort over at most 1+kMaxSideDetectors ints does. */
        int sfs[1 + kMaxSideDetectors] = {s.sf};
        int n = 1;
        for (int i = 0; i < s.nSide && i < kMaxSideDetectors; i++) sfs[n++] = s.side[i].sf;
        for (int i = 1; i < n; i++) {
            int v = sfs[i], j = i;
            for (; j > 0 && sfs[j - 1] > v; j--) sfs[j] = sfs[j - 1];
            sfs[j] = v;
        }
        at += (size_t)snprintf(line + at, sizeof line - at, ",\"sfs\":[");
        for (int i = 0; i < n; i++)
            at += (size_t)snprintf(line + at, sizeof line - at, "%s%d", i ? "," : "", sfs[i]);
        at += (size_t)snprintf(line + at, sizeof line - at, "]");
    }
    at += (size_t)snprintf(line + at, sizeof line - at, "}");
    sendLine(line, at);
}

void etherPublishTx(const EtherTxFrame& f)
{
    if (s_fd < 0) return;
    char payload[4 * ((kMaxPayload + 2) / 3) + 1];
    simradio_json::base64Encode(f.payload, f.len, payload, sizeof payload);

    char line[1024];
    size_t at = (size_t)snprintf(line, sizeof line,
        "{\"type\":\"tx\",\"sid\":%d,\"t\":%lld,\"id\":%d,"
        "\"t0\":%lld,\"t_pre\":%lld,\"t_hdr\":%lld,\"t_end\":%lld,\"power_dbm\":%d,",
        s_sid, (long long)S()->now_us(), f.id,
        (long long)f.t0, (long long)f.tPre, (long long)f.tHdr, (long long)f.tEnd,
        f.powerDbm);
    appendState(line, sizeof line, &at, f.state);
    at += (size_t)snprintf(line + at, sizeof line - at, ",\"payload\":\"%s\"}", payload);
    sendLine(line, at);
}

/* ---- Bring-up ---- */

extern "C" int simradio_station_open(int sid, const char* bind_addr, const char* ether_addr)
{
    S()->lock();
    if (s_started) { S()->unlock(); return 0; }
    s_started = true;
    s_sid = sid;
    S()->unlock();

    if (!ether_addr || !*ether_addr) {
        S()->log(SIMRADIO_LOG_INFO, "ether: none named, the radio transmits into nothing");
        return 0;
    }

    int fd = S()->udp_open(bind_addr && *bind_addr ? bind_addr : "127.0.0.1", ether_addr);
    if (fd < 0) {
        S()->log(SIMRADIO_LOG_ERROR, "ether: cannot reach %s", ether_addr);
        return -1;
    }
    s_fd = fd;
    conductor::setIdleSender(sendIdle);
    conductor::start();

    if (S()->spawn_reader(fd, handleDatagram) != 0) {
        S()->log(SIMRADIO_LOG_ERROR, "ether: no reader");
        return -1;
    }

    char hello[160];
    int n = snprintf(hello, sizeof hello,
        "{\"type\":\"hello\",\"sid\":%d,\"t\":%lld,\"slots\":[0],\"lines\":1}",
        s_sid, (long long)S()->now_us());
    sendRaw(hello, (size_t)n);
    S()->log(SIMRADIO_LOG_INFO, "ether: %s, station %d", ether_addr, s_sid);
    if (conductor::isVirtual()) {
        /* Not back to the host before the welcome has set T. What the host
         * does next reads node time, and until the welcome that is 0, not
         * the instant the station joins at: a sleep begun before the welcome
         * landed ended at another millisecond than one begun after it, so a
         * station's first steps depended on which of two threads the host
         * ran first. Raw sleeps: the time shim's would wait on T. */
        struct timespec ms = {0, 1000 * 1000};
        for (int i = 0; i < kJoinWaitMs && !conductor::joined(); i++)
            syscall(SYS_nanosleep, &ms, nullptr);
        if (!conductor::joined())
            S()->log(SIMRADIO_LOG_WARN, "ether: no welcome in %d ms; going on without it",
                     kJoinWaitMs);
    }
    return 0;
}
