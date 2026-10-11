/**
 * conductor — see the header.
 */
#include "conductor.h"

#include "simclock.h"
#include "simradio.h"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <sys/syscall.h>
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace conductor {
namespace {

const struct simradio_services* B() { return simradio_services(); }

/* How long a grant may go unanswered, in wall time, before this station
 * reports idle anyway. A station whose tasks never all block — a spin, a wait
 * in a call nothing here can see — is then given time at the pace it actually
 * runs, rather than stopping the run. */
constexpr long kBusyGraceNs = 20 * 1000 * 1000;

/* How long, in wall time, an idle goes unanswered before it is said again
 * (resendIdle in the header). */
constexpr long kResendNs = 250 * 1000 * 1000;

/* Strict time (SIM_MESH_STRICT_TIME=1): the busy grace never answers for a
 * station still at work. Without it, a station busy past the grace in wall
 * time (a proof of work, a spin) has T moved under it by however long this
 * host took, and two runs of one package part there. With it, the grace
 * answers only once none of the station's threads is running: one asleep in
 * a wait the census cannot see is idle, and is answered as before, at the
 * same T whenever the host gets to it. A thread that never stops running
 * stops the run, and says so every kStrictSayS of wall time. */
constexpr long kStrictSayS = 10;

/* Strict time: how many reads of one instant make a thread one that waits for
 * the clock to move (readNowUs). Handling a packet or an event reads the clock
 * a few times, and at a thousand a working loop of reticulum's stations
 * already reached it in a city run; a loop that waits on the clock reaches a
 * hundred thousand in a fraction of a second of the host's time. Once a
 * thread has been found so, kSpinStepReads reads of each next instant are
 * enough while it goes on waiting there: each step of T costs the run a
 * barrier, and a hundred thousand reads a step made a tool waiting on the
 * wall clock give up on a station mid-turn. */
constexpr int kSpinReads = 100000;
constexpr int kSpinStepReads = 1000;

std::atomic<int>     s_mode{-1};
std::atomic<int64_t> s_T{0};
std::atomic<int64_t> s_epoch{0};
std::atomic<bool>    s_joined{false};
std::atomic<int64_t> s_nodeAtJoin{0};
std::atomic<uint64_t> s_seq{0};
std::atomic<bool>    s_owed{false};
std::atomic<bool>    s_resending{false};   /* an idle is out and nothing has come back */
std::atomic<int64_t> s_armedUs{0};         /* wall clock when the busy grace was armed */
std::atomic<int64_t> s_until{kNever};
std::atomic<int64_t> s_chipNext{kNever};   /* the earliest armed timer, in T */
std::atomic<int64_t> s_lastUntil{kNever};
void               (*s_sendIdle)(uint64_t, int64_t, uint64_t) = nullptr;
std::atomic<uint64_t> s_idleN{0};          /* idles said; one said again keeps its number */
std::atomic<void (*)(void)> s_onAdvance{nullptr};
std::atomic<bool>    s_holding{false};  /* a datagram is being applied (hold) */
bool                 s_hookOwed = false; /* T moved during it; under B()->lock() */
thread_local bool    t_grants = false;   /* this thread applies the ether's grants */
int                  s_tfd = -1;

/* The real wall clock, past the time shim. */
int64_t rawWallUs()
{
    struct timespec ts;
    syscall(SYS_clock_gettime, CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* ---- f: the node's clock as a function of T ----
 *
 * SIM_MESH_CLOCK_PROFILE, when set, is "T:node,T:node,…" in microseconds,
 * both columns increasing: a monotone piecewise-linear map, slope 1 outside
 * the points. Absent, f is the identity. */
struct Point { int64_t t, n; };

const std::vector<Point>& profile()
{
    static const std::vector<Point> points = [] {
        std::vector<Point> out;
        const char* v = getenv("SIM_MESH_CLOCK_PROFILE");
        while (v && *v) {
            char* end = nullptr;
            long long t = strtoll(v, &end, 10);
            if (end == v || *end != ':') break;
            v = end + 1;
            long long n = strtoll(v, &end, 10);
            if (end == v) break;
            if (!out.empty() && (t <= out.back().t || n <= out.back().n)) break;
            out.push_back(Point{t, n});
            v = *end == ',' ? end + 1 : end;
            if (*end != ',') break;
        }
        return out;
    }();
    return points;
}

/* Forward, from T to node time, rounds down; back, from node time to T,
 * rounds up, so a wake the host asks for at node time n comes back as the
 * first T whose node time has reached n. Rounded down both ways, a slope that
 * is not a whole ratio — a crystal a few ppm off — handed the host its wake a
 * microsecond early: it found its deadline not reached, asked for the same
 * instant again, and moved on only by the ether's 10 ms guard. */
int64_t mapThrough(const std::vector<Point>& p, int64_t x, bool forward)
{
    auto from = [&](const Point& q) { return forward ? q.t : q.n; };
    auto to   = [&](const Point& q) { return forward ? q.n : q.t; };
    if (p.empty()) return x;
    if (x == kNever) return kNever;
    if (x <= from(p.front())) return to(p.front()) + (x - from(p.front()));
    for (size_t i = 1; i < p.size(); i++) {
        if (x <= from(p[i])) {
            int64_t dx = from(p[i]) - from(p[i - 1]);
            int64_t dy = to(p[i]) - to(p[i - 1]);
            __int128 num = (__int128)(x - from(p[i - 1])) * dy;     /* > 0 here */
            return to(p[i - 1]) + (int64_t)(forward ? num / dx : (num + dx - 1) / dx);
        }
    }
    return to(p.back()) + (x - from(p.back()));
}

/* ---- The model's timers, in T ---- */

struct Timer {
    void      (*cb)(void*);
    void*       arg;
    void*       real;       /* the backend's, in a real-time run */
    int64_t     at;
    bool        armed;
};

struct Wake {
    void      (*cb)(void*);
    void*       arg;
    int64_t     atNode;
};

std::vector<Timer*>& timers() { static auto* v = new std::vector<Timer*>(); return *v; }
std::vector<Wake>&   wakes()  { static auto* v = new std::vector<Wake>();   return *v; }

/* Under the lock. */
int64_t computeUntil()
{
    int64_t until = kNever;
    for (Timer* t : timers())
        if (t->armed) until = std::min(until, t->at);
    s_chipNext.store(until);
    for (const Wake& w : wakes())
        if (w.atNode != kNever) until = std::min(until, conductorOf(w.atNode));
    s_until.store(until);
    return until;
}

int64_t modelNow()
{
    return isVirtual() ? s_T.load() : B()->now_us();
}

void* timerCreate(void (*cb)(void*), void* arg, const char* name)
{
    Timer* t = new Timer{cb, arg, nullptr, 0, false};
    if (!isVirtual()) {
        t->real = B()->timer_create(cb, arg, name);
        return t;
    }
    B()->lock();
    timers().push_back(t);
    B()->unlock();
    return t;
}

void timerStartOnce(void* timer, int64_t delayUs)
{
    Timer* t = (Timer*)timer;
    if (!t) return;
    if (!isVirtual()) { B()->timer_start_once(t->real, delayUs); return; }
    B()->lock();
    t->at = s_T.load() + (delayUs < 0 ? 0 : delayUs);
    t->armed = true;
    computeUntil();
    B()->unlock();
}

void timerStop(void* timer)
{
    Timer* t = (Timer*)timer;
    if (!t) return;
    if (!isVirtual()) { B()->timer_stop(t->real); return; }
    B()->lock();
    t->armed = false;
    computeUntil();
    B()->unlock();
}

void lock()   { B()->lock(); }
void unlock() { B()->unlock(); }

int udpOpen(const char* bindAddr, const char* dest) { return B()->udp_open(bindAddr, dest); }

int spawnReader(int fd, void (*onDatagram)(const char*, size_t))
{
    return B()->spawn_reader(fd, onDatagram);
}

/* ---- The busy watchdog ---- */

void armWatchdog()
{
    s_armedUs.store(rawWallUs());
    if (s_tfd < 0) return;
    struct itimerspec its = {};
    its.it_value.tv_nsec = kBusyGraceNs;
    timerfd_settime(s_tfd, 0, &its, nullptr);
}

bool strictTime()
{
    static const bool on = [] {
        const char* e = getenv("SIM_MESH_STRICT_TIME");
        return e && *e == '1';
    }();
    return on;
}

/* Strict time, a station still at work: look again a grace on. */
void armStrictLook()
{
    if (s_tfd < 0) return;
    struct itimerspec its = {};
    its.it_value.tv_nsec = kBusyGraceNs;
    timerfd_settime(s_tfd, 0, &its, nullptr);
}

/* Is any thread of this station but the asking one running, or ready to?
 * /proc's state R; a thread asleep, in any wait, is not at work. */
bool othersRunning()
{
    pid_t self = (pid_t)syscall(SYS_gettid);
    DIR* d = opendir("/proc/self/task");
    if (!d) return true;   /* cannot tell: hold T, as strict time does */
    bool any = false;
    while (struct dirent* e = readdir(d)) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9' || atoi(e->d_name) == self) continue;
        char path[300], buf[512];
        snprintf(path, sizeof path, "/proc/self/task/%s/stat", e->d_name);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) continue;
        ssize_t n = read(fd, buf, sizeof buf - 1);
        close(fd);
        if (n <= 0) continue;
        buf[n] = 0;
        const char* r = strrchr(buf, ')');   /* the name may hold spaces and parentheses */
        if (r && r[1] == ' ' && r[2] == 'R') { any = true; break; }
    }
    closedir(d);
    return any;
}

/* After an idle: the timer says it again kResendNs on unless the ether has
 * answered or the station has spoken first. */
void armResend()
{
    s_resending.store(true);
    if (s_tfd < 0) return;
    struct itimerspec its = {};
    its.it_value.tv_nsec = kResendNs;
    timerfd_settime(s_tfd, 0, &its, nullptr);
}

void sendIdleNow(int64_t until)
{
    s_lastUntil.store(until);
    uint64_t n = s_idleN.fetch_add(1) + 1;
    if (s_sendIdle) s_sendIdle(s_seq.load(), until, n);
}

void* watchdogMain(void*)
{
    sigset_t all;
    sigfillset(&all);
    pthread_sigmask(SIG_BLOCK, &all, nullptr);
    for (;;) {
        uint64_t n;
        if (read(s_tfd, &n, sizeof n) != (ssize_t)sizeof n) continue;
        if (strictTime() && s_owed.load() && othersRunning()) {
            static int64_t said = 0;
            int64_t now = rawWallUs(), busy = (now - s_armedUs.load()) / 1000000;
            if (busy >= kStrictSayS && now - said >= kStrictSayS * 1000000) {
                said = now;
                B()->log(SIMRADIO_LOG_WARN, "conductor: at work %lld s of wall time on one grant, "
                         "and strict time holds T for it", (long long)busy);
            }
            armStrictLook();
            continue;
        }
        if (s_owed.exchange(false)) {
            sendIdleNow(s_until.load());
            armResend();
        } else if (s_resending.load()) {
            if (s_sendIdle) s_sendIdle(s_seq.load(), s_lastUntil.load(), s_idleN.load());
            armResend();
        }
    }
    return nullptr;
}

void startWatchdog()
{
    s_tfd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC);
    if (s_tfd < 0) {
        B()->log(SIMRADIO_LOG_WARN, "conductor: timerfd: %s", strerror(errno));
        return;
    }
    /* Created with every signal blocked, so that on a host whose tasks are
     * signal-driven this thread can never be where a signal lands. */
    sigset_t all, old;
    sigfillset(&all);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    pthread_t th;
    int rc = pthread_create(&th, nullptr, watchdogMain, nullptr);
    pthread_sigmask(SIG_SETMASK, &old, nullptr);
    if (rc != 0) {
        B()->log(SIMRADIO_LOG_WARN, "conductor: no watchdog thread");
        close(s_tfd);
        s_tfd = -1;
        return;
    }
    pthread_detach(th);
}

/* ---- The time shim ---- */

/* Node time as the station's own code reads it: simradio_node_us, and the
 * clocks the time shim answers. Under strict time T moves only when the
 * station is idle, and a thread that waits for time by reading the clock in a
 * loop never is: T would wait for it, and it for T. So a thread's
 * kSpinReads-th read of one instant first sleeps the shortest sleep there is,
 * which the time shim ends at the next instant a wait can end on: the next
 * whole millisecond of node time, or the chips' next event before it with
 * SIM_MESH_SHORT_WAITS=chip. While each of its instants ends in such a
 * sleep, kSpinStepReads reads of the next one do too; one that ends any other
 * way (the thread waited elsewhere) takes kSpinReads again. When a thread
 * waits, and what it reads, then follow from its own reads alone, never from
 * the host's pace: work that
 * reads the clock fewer times at one instant takes no time of T's, as before,
 * and work that never reads it holds T until it is done. The thread that
 * applies the ether's grants never sleeps here, since only it moves T. */
int64_t readNowUs()
{
    int64_t now = nodeNowUs();
    if (!strictTime() || !isVirtual() || t_grants || !s_joined.load()) return now;
    static thread_local int64_t last = -1;
    static thread_local int same = 0;
    static thread_local bool waiting = false;   /* its last instant ended in a sleep here */
    if (now != last) {
        last = now;
        same = 0;
        waiting = false;
        return now;
    }
    if (++same < (waiting ? kSpinStepReads : kSpinReads)) return now;
    same = 0;              /* the log below reads the clock too */
    static thread_local bool said = false;
    if (!said) {
        said = true;
        B()->log(SIMRADIO_LOG_WARN, "conductor: a thread read node time %d times at %lld us; "
                 "strict time sleeps it until the next instant a wait ends on (said once a thread)",
                 kSpinReads, (long long)now);
    }
    usleep(1);
    now = nodeNowUs();
    if (now != last) {
        last = now;
        same = 0;
        waiting = true;
    } else {
        same = kSpinReads - 1;   /* a signal ended the sleep: the next read sleeps again */
    }
    return now;
}

int64_t opsNodeUs() { return nodeNowUs(); }
int64_t opsReadUs() { return readNowUs(); }
void    opsSpoke() { spoke(); }
int64_t opsEpochUs() { return epochUs(); }
int     opsWakeCreate(void (*due)(void*), void* arg) { return wakeCreate(due, arg); }
void    opsWakeAt(int w, int64_t node) { wakeAt(w, node); }
void    opsIdle() { idle(); }

/* The chips' next event as a wait would end on it: the first node time whose T
 * has reached the earliest armed timer. Read without the lock, which every
 * short wait of every thread would otherwise take. */
int64_t opsChipNextUs()
{
    int64_t t = s_chipNext.load();
    if (t == kNever) return kNever;
    int64_t n = firstNodeAt(t);
    return conductorOf(n) < t ? n + 1 : n;
}

const struct simclock_ops kOps = { opsNodeUs, opsEpochUs, opsWakeCreate, opsWakeAt, opsIdle,
                                   opsChipNextUs, opsSpoke, opsReadUs };

void attachShim()
{
    /* With every signal held off: dlsym takes the dynamic linker's lock, and a
     * host whose tasks are switched by signals must not switch this one out
     * while it holds it. */
    sigset_t all, old;
    sigfillset(&all);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    auto attach = (simclock_attach_fn)dlsym(RTLD_DEFAULT, "simclock_attach");
    pthread_sigmask(SIG_SETMASK, &old, nullptr);
    if (attach) attach(&kOps);
    else B()->log(SIMRADIO_LOG_WARN, "conductor: virtual time without the time shim; "
                                     "the host's own clocks run in real time");
}

struct simradio_services s_model;

}  // namespace

bool isVirtual()
{
    int m = s_mode.load();
    if (m < 0) {
        const char* v = getenv("SIM_MESH_TIME");
        m = (v && strcmp(v, "virtual") == 0) ? 1 : 0;
        int expected = -1;
        if (!s_mode.compare_exchange_strong(expected, m)) m = expected;
        else if (m) {
            const char* e = getenv("SIM_MESH_EPOCH_US");
            s_epoch.store(e && *e ? strtoll(e, nullptr, 10) : rawWallUs());
        }
    }
    return m == 1;
}

const struct simradio_services* modelServices()
{
    static const struct simradio_services* made = [] {
        s_model = *B();
        s_model.now_us = modelNow;
        s_model.timer_create = timerCreate;
        s_model.timer_start_once = timerStartOnce;
        s_model.timer_stop = timerStop;
        s_model.lock = lock;
        s_model.unlock = unlock;
        s_model.udp_open = udpOpen;
        s_model.spawn_reader = spawnReader;
        return &s_model;
    }();
    return made;
}

int64_t nodeOf(int64_t t)       { return mapThrough(profile(), t, true); }
int64_t conductorOf(int64_t n)  { return mapThrough(profile(), n, false); }

int64_t firstNodeAt(int64_t t)
{
    /* nodeOf rounds down, so its answer can be a microsecond or two past the
     * first node time that maps back to `t` or later; step back to it. */
    int64_t n = nodeOf(t);
    while (n > 0 && conductorOf(n - 1) >= t) n--;
    return n;
}

int64_t nodeNowUs()
{
    return isVirtual() ? nodeOf(s_T.load()) : B()->now_us();
}

int64_t epochUs()
{
    if (isVirtual()) return s_epoch.load();
    static const int64_t e = rawWallUs() - B()->now_us();
    return e;
}

/* Everything due by T, in time order: the chip's timers, and the host's waits
 * unless a datagram is being applied (hold), whose waits wait for release. */
void runDue()
{
    B()->lock();
    for (;;) {
        int64_t now = s_T.load();
        int64_t best = kNever;
        Timer* timer = nullptr;
        int wake = -1;
        for (Timer* tm : timers())
            if (tm->armed && tm->at <= now && tm->at < best) { best = tm->at; timer = tm; }
        for (size_t i = 0; i < wakes().size() && !s_holding.load(); i++) {
            Wake& w = wakes()[i];
            if (w.atNode == kNever) continue;
            int64_t at = conductorOf(w.atNode);
            if (at <= now && at < best) { best = at; timer = nullptr; wake = (int)i; }
        }
        void (*cb)(void*) = nullptr;
        void* arg = nullptr;
        if (timer) {
            timer->armed = false;
            cb = timer->cb;
            arg = timer->arg;
        } else if (wake >= 0) {
            wakes()[wake].atNode = kNever;
            cb = wakes()[wake].cb;
            arg = wakes()[wake].arg;
        } else {
            break;
        }
        computeUntil();
        B()->unlock();
        cb(arg);
        B()->lock();
    }
    computeUntil();
    B()->unlock();
}

void advanceTo(int64_t t)
{
    if (!isVirtual()) return;
    t_grants = true;
    B()->lock();
    bool moved = t > s_T.load();
    if (moved) s_T.store(t);
    bool holding = s_holding.load();
    if (moved && holding) s_hookOwed = true;
    B()->unlock();
    void (*hook)(void) = s_onAdvance.load();
    if (moved && hook && !holding) hook();
    runDue();
}

void hold()
{
    t_grants = true;
    s_holding.store(true);
}

void release()
{
    s_holding.store(false);
    B()->lock();
    bool owed = s_hookOwed;
    s_hookOwed = false;
    B()->unlock();
    void (*hook)(void) = s_onAdvance.load();
    if (owed && hook) hook();
    runDue();
}

void welcome(bool isVirtualRun, int64_t t, int64_t epoch, uint64_t seq)
{
    if (isVirtualRun != isVirtual()) {
        B()->log(SIMRADIO_LOG_ERROR, "conductor: the ether runs in %s time and this "
                 "station was started for %s", isVirtualRun ? "virtual" : "real",
                 isVirtual() ? "virtual" : "real");
        return;
    }
    if (!isVirtual()) return;
    if (epoch) s_epoch.store(epoch);
    advanceTo(t);
    s_nodeAtJoin.store(nodeOf(s_T.load()));
    s_joined.store(true);
    granted(seq);
}

int64_t nodeAtJoin() { return s_nodeAtJoin.load(); }
bool    joined()     { return s_joined.load(); }

void granted(uint64_t seq)
{
    if (!isVirtual()) return;
    s_seq.store(seq);
    s_resending.store(false);
    s_owed.store(true);
    armWatchdog();
}

void spoke()
{
    if (!isVirtual() || !s_joined.load()) return;
    s_resending.store(false);
    s_owed.store(true);
    armWatchdog();
}

uint64_t lastSeq() { return s_seq.load(); }

void resendIdle()
{
    if (!isVirtual() || !s_joined.load() || !s_sendIdle) return;
    s_sendIdle(s_seq.load(), s_lastUntil.load(), s_idleN.load());
}

int wakeCreate(void (*due)(void*), void* arg)
{
    B()->lock();
    wakes().push_back(Wake{due, arg, kNever});
    int id = (int)wakes().size() - 1;
    B()->unlock();
    return id;
}

void wakeAt(int wake, int64_t nodeUs)
{
    B()->lock();
    if (wake >= 0 && wake < (int)wakes().size()) {
        wakes()[wake].atNode = nodeUs;
        computeUntil();
    }
    B()->unlock();
}

void idle()
{
    if (!isVirtual() || !s_joined.load()) return;
    B()->lock();
    int64_t until = computeUntil();
    B()->unlock();
    bool owed = s_owed.exchange(false);
    if (owed || until < s_lastUntil.load()) {
        sendIdleNow(until);
        armResend();
    }
}

void setIdleSender(void (*send)(uint64_t, int64_t, uint64_t))
{
    s_sendIdle = send;
}

void onAdvance(void (*moved)(void))
{
    s_onAdvance.store(moved);
}

void start()
{
    if (!isVirtual()) return;
    startWatchdog();
    attachShim();
}

}  // namespace conductor

extern "C" int simradio_virtual(void) { return conductor::isVirtual() ? 1 : 0; }
extern "C" int64_t simradio_node_us(void) { return conductor::readNowUs(); }
extern "C" int64_t simradio_epoch_us(void) { return conductor::epochUs(); }
extern "C" int64_t simradio_node_to_conductor(int64_t node) { return conductor::conductorOf(node); }
extern "C" int simradio_wake_create(void (*due)(void*), void* arg) { return conductor::wakeCreate(due, arg); }
extern "C" void simradio_wake_at(int wake, int64_t node_us) { conductor::wakeAt(wake, node_us); }
extern "C" void simradio_idle(void) { conductor::idle(); }
extern "C" void simradio_on_advance(void (*moved)(void)) { conductor::onAdvance(moved); }
extern "C" int simradio_joined(void) { return conductor::joined() ? 1 : 0; }
extern "C" int64_t simradio_node_at_join(void) { return conductor::nodeAtJoin(); }

/* The host's services, once it has handed them over; the library's own until then. */
static std::atomic<const struct simradio_services*> s_host{nullptr};

extern "C" void simradio_set_services(const struct simradio_services* services)
{
    s_host.store(services);
}

extern "C" const struct simradio_services* simradio_services(void)
{
    const struct simradio_services* host = s_host.load();
    return host ? host : simradio_posix_services();
}
