/**
 * libsimclock.so — the C library's time, answered in node time.
 *
 * Preloaded into a station started for a virtual-time run (SIM_MESH_TIME=
 * virtual). The station's chip library owns the clock; when it starts it finds
 * simclock_attach here and hands over what this needs (simclock.h). From then
 * on:
 *
 *   clock_gettime, gettimeofday, time        node time (+ the run's epoch for
 *                                            the wall clocks)
 *   nanosleep, clock_nanosleep, usleep, sleep
 *                                            until node time reaches the end, or
 *                                            a signal, as the real ones
 *   setitimer / getitimer (ITIMER_REAL)      SIGALRM once per interval of node
 *                                            time, on multiples of the interval
 *   poll, ppoll, select, pselect, epoll_wait, epoll_pwait
 *                                            the descriptors, or node time
 *                                            reaching the timeout
 *   pthread_cond_timedwait, pthread_cond_clockwait
 *                                            a signal, or node time reaching the
 *                                            deadline (ETIMEDOUT); a timedwait's
 *                                            deadline below the run's epoch is
 *                                            on the monotonic clock, the one a
 *                                            condition made with
 *                                            pthread_condattr_setclock keeps
 *   sem_timedwait, sem_clockwait            a post, a signal, or node time
 *                                            reaching the deadline (ETIMEDOUT):
 *                                            what CPython's locks wait in
 *
 * A wait blocks on a descriptor of its own thread's (an eventfd), which a wake
 * the chip library runs when a grant reaches the instant writes to. So a thread
 * waiting in node time is woken by the grant, and a signal still ends its wait
 * the way it ends a real one — which a host whose threads are switched by
 * signals depends on. A wait ends on a whole millisecond of node time, and
 * with SIM_MESH_SHORT_WAITS=chip one shorter than that ends at the chips' next
 * event when that comes first (wait_end).
 *
 * With SIM_MESH_IDLE=threads the shim also keeps a census of the process's
 * threads: every thread created through pthread_create, and the first. A
 * thread is blocked while it is inside one of the waits above, an untimed
 * pthread_cond_wait or sem_wait, or a read, recv or accept on a blocking
 * descriptor that is not a file (a regular file, directory or disk never
 * waits on anything outside the process); when the last one blocks, and none
 * of the descriptors the blocked threads wait on has anything ready for them,
 * the station is idle, and the shim says so to the chip library, whose wakes
 * already hold every deadline the blocked threads are waiting for. A wake
 * that fires counts its thread as running at once, and so does a
 * pthread_cond_signal or pthread_cond_broadcast every thread waiting on that
 * condition, and a sem_post every thread waiting on that semaphore, so the
 * station cannot look idle between the wake and the thread getting the CPU.
 *
 * A process with no radio of its own joins the run the same way, as a
 * station without a slot: it opens its link (simradio_station_open) and
 * never opens a chip: a Python host talking to a radio in another process of
 * its station, say.
 *
 * With SIM_MESH_SEED in the environment as well, the shim is also the station's
 * randomness: getentropy, getrandom and syscall(SYS_getrandom) — what ESP-IDF's
 * host esp_random and mbedtls's platform entropy call — draw from a generator
 * keyed by the seed and SIM_MESH_NODE_ID, so a run given the same seed gives
 * every station the same bytes in the same order, and two stations of one run
 * different ones. This needs nothing from the chip library and holds from the
 * process's first instruction.
 *
 * In a virtual-time run the shim also counts the bytes that reach the station
 * from outside the air, and the bytes it sends to another station over TCP,
 * and tells the ether, on the chip library's own socket to it, once the
 * station has said hello on it:
 *
 *   {"type":"read","ch":"tty","total":N}      N bytes read from the console
 *                                             (descriptor 0) since the start,
 *                                             once a read leaves none waiting
 *   {"type":"wrote","ch":"tcp/A>B","n":N,"go":k}
 *                                             N bytes about to be written on
 *                                             the TCP connection from A to B
 *                                             ("addr:port"): asked before the
 *                                             call, on the writing thread's
 *                                             own socket to SIM_MESH_ETHER, and
 *                                             the call waits for {"go":k};
 *                                             after it, without "go", minus
 *                                             what the call did not take
 *   {"type":"read","ch":"tcp/A>B","n":N}      N bytes read from it
 *   {"type":"listen","at":"addr:port"}        the station listens there, so a
 *                                             connection to that end is its,
 *                                             whichever other process of the
 *                                             run shares the address
 *
 * The ether holds T while a channel has bytes its reader has not taken, and
 * lets a TCP write go when its reader is in step (ether/INTERNALS.md). A
 * report travels on the same socket as the idle that follows it, so it is
 * always ahead of it; a TCP write is asked for before its bytes exist. A TCP
 * connection to a loopback address leaves from SIM_MESH_BIND_ADDR, so both of
 * its ends name a station. And while the station computes, the chip
 * library's busy watchdog is held back (below).
 *
 * Without SIM_MESH_TIME=virtual, and before the chip library attaches, every
 * clock and wait is the C library's own; without SIM_MESH_TIME=virtual or
 * without SIM_MESH_SEED, so is the randomness.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <semaphore.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/ioctl.h>
#include <sys/random.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>

#include "simclock.h"

#define NEVER INT64_MAX

static int s_virtual = -1;
static int s_census;
static int s_shortWaits;
static int64_t s_epochEnv;
static const struct simclock_ops* _Atomic s_ops;

/* ---- The C library's own ---- */

static void* real_sym(void** slot, const char* name)
{
    void* p = *slot;
    if (!p) *slot = p = dlsym(RTLD_NEXT, name);
    return p;
}

#define REAL(name) ((__typeof__(r_##name))real_sym((void**)&r_##name, #name))

static int (*r_clock_gettime)(clockid_t, struct timespec*);
static int (*r_gettimeofday)(struct timeval*, void*);
static time_t (*r_time)(time_t*);
static int (*r_nanosleep)(const struct timespec*, struct timespec*);
static int (*r_clock_nanosleep)(clockid_t, int, const struct timespec*, struct timespec*);
static int (*r_usleep)(useconds_t);
static unsigned (*r_sleep)(unsigned);
static int (*r_setitimer)(__itimer_which_t, const struct itimerval*, struct itimerval*);
static int (*r_getitimer)(__itimer_which_t, struct itimerval*);
static int (*r_poll)(struct pollfd*, nfds_t, int);
static int (*r_ppoll)(struct pollfd*, nfds_t, const struct timespec*, const sigset_t*);
static int (*r_select)(int, fd_set*, fd_set*, fd_set*, struct timeval*);
static int (*r_pselect)(int, fd_set*, fd_set*, fd_set*, const struct timespec*, const sigset_t*);
static int (*r_epoll_wait)(int, struct epoll_event*, int, int);
static int (*r_epoll_pwait)(int, struct epoll_event*, int, int, const sigset_t*);
static int (*r_pthread_cond_timedwait)(pthread_cond_t*, pthread_mutex_t*, const struct timespec*);
static int (*r_pthread_cond_clockwait)(pthread_cond_t*, pthread_mutex_t*, clockid_t, const struct timespec*);
static int (*r_pthread_cond_wait)(pthread_cond_t*, pthread_mutex_t*);
static int (*r_pthread_cond_signal)(pthread_cond_t*);
static int (*r_pthread_cond_broadcast)(pthread_cond_t*);
static int (*r_pthread_create)(pthread_t*, const pthread_attr_t*, void* (*)(void*), void*);
static int (*r_sem_wait)(sem_t*);
static int (*r_sem_trywait)(sem_t*);
static int (*r_sem_timedwait)(sem_t*, const struct timespec*);
static int (*r_sem_clockwait)(sem_t*, clockid_t, const struct timespec*);
static int (*r_sem_post)(sem_t*);
static int (*r_listen)(int, int);
static ssize_t (*r_read)(int, void*, size_t);
static ssize_t (*r_readv)(int, const struct iovec*, int);
static ssize_t (*r_write)(int, const void*, size_t);
static ssize_t (*r_writev)(int, const struct iovec*, int);
static ssize_t (*r_send)(int, const void*, size_t, int);
static ssize_t (*r_sendto)(int, const void*, size_t, int, const struct sockaddr*, socklen_t);
static ssize_t (*r_sendmsg)(int, const struct msghdr*, int);
static ssize_t (*r_recv)(int, void*, size_t, int);
static ssize_t (*r_recvfrom)(int, void*, size_t, int, struct sockaddr*, socklen_t*);
static ssize_t (*r_recvmsg)(int, struct msghdr*, int);
static int (*r_accept)(int, struct sockaddr*, socklen_t*);
static int (*r_accept4)(int, struct sockaddr*, socklen_t*, int);
static int (*r_connect)(int, const struct sockaddr*, socklen_t);
static int (*r_getentropy)(void*, size_t);
static ssize_t (*r_getrandom)(void*, size_t, unsigned);
static long (*r_syscall)(long, ...);

static int s_seeded;                /* the randomness is ours */
static uint64_t s_randKey;          /* from SIM_MESH_SEED and SIM_MESH_NODE_ID */
static int s_sid;                   /* SIM_MESH_NODE_ID */

static uint64_t fnv(uint64_t h, const char* s)
{
    for (; *s; s++) { h ^= (unsigned char)*s; h *= 1099511628211ULL; }
    return h;
}

static void load_mode(void)
{
    if (s_virtual >= 0) return;
    const char* v = getenv("SIM_MESH_TIME");
    const char* i = getenv("SIM_MESH_IDLE");
    const char* e = getenv("SIM_MESH_EPOCH_US");
    const char* seed = getenv("SIM_MESH_SEED");
    const char* id = getenv("SIM_MESH_NODE_ID");
    const char* w = getenv("SIM_MESH_SHORT_WAITS");
    int virt = v && strcmp(v, "virtual") == 0;
    s_census = i && strcmp(i, "threads") == 0;
    s_shortWaits = w && strcmp(w, "chip") == 0;
    s_epochEnv = e && *e ? strtoll(e, NULL, 10) : 0;
    s_sid = id ? atoi(id) : 0;
    if (virt && seed && *seed) {
        s_randKey = fnv(fnv(fnv(1469598103934665603ULL, seed), "/"), id ? id : "");
        s_seeded = 1;
    }
    s_virtual = virt;
}

/* The clock, when it is ours to answer; NULL means "the C library's". */
static const struct simclock_ops* ops(void)
{
    load_mode();
    if (!s_virtual) return NULL;
    return atomic_load(&s_ops);
}

static int64_t ts_us(const struct timespec* ts) { return (int64_t)ts->tv_sec * 1000000 + ts->tv_nsec / 1000; }
static void us_ts(int64_t us, struct timespec* ts) { ts->tv_sec = us / 1000000; ts->tv_nsec = (us % 1000000) * 1000; }
static int64_t tv_us(const struct timeval* tv) { return (int64_t)tv->tv_sec * 1000000 + tv->tv_usec; }
static void us_tv(int64_t us, struct timeval* tv) { tv->tv_sec = us / 1000000; tv->tv_usec = us % 1000000; }

static int monotonic_clock(clockid_t c)
{
    return c == CLOCK_MONOTONIC || c == CLOCK_MONOTONIC_RAW || c == CLOCK_MONOTONIC_COARSE ||
           c == CLOCK_BOOTTIME;
}

static int wall_clock(clockid_t c)
{
    return c == CLOCK_REALTIME || c == CLOCK_REALTIME_COARSE || c == CLOCK_TAI;
}

/* A wait's end, on the timer resolution: the next whole millisecond of node
 * time. Every instant a station asks for costs the whole run one barrier, and a
 * driver that sleeps ten microseconds at a time in a loop would otherwise ask
 * for a hundred of them per millisecond; aligned, the ends of many threads' and
 * many stations' waits also fall on the same instants. */
#define RESOLUTION_US 1000

static int64_t resolve(int64_t deadline)
{
    if (deadline == NEVER || deadline <= 0) return deadline;
    return ((deadline + RESOLUTION_US - 1) / RESOLUTION_US) * RESOLUTION_US;
}

/* With SIM_MESH_SHORT_WAITS=chip, a wait shorter than the resolution ends at
 * the chips' next event (simclock_ops.chip_next_us) when that comes before
 * the millisecond it would end on, or at its own end when the event comes
 * sooner still. A driver that polls BUSY in 10 µs sleeps then sees it fall
 * when it falls, as on a board, at an instant the run stops at anyway, where
 * on the millisecond every command it waits out costs it up to one more. */
static int64_t wait_end(const struct simclock_ops* o, int64_t deadline)
{
    int64_t end = resolve(deadline);
    if (!s_shortWaits || end == deadline || deadline - o->node_us() >= RESOLUTION_US)
        return end;
    int64_t next = o->chip_next_us();
    if (next >= end) return end;
    return next > deadline ? next : deadline;
}

/* Node time now, on a clock's own scale. Before the chip library attaches it
 * is 0: the station's time has not started, and a clock that read the host's
 * until then would run backwards when it did. */
static int64_t clock_now(const struct simclock_ops* o, clockid_t c)
{
    int64_t n = o ? (o->read_us ? o->read_us() : o->node_us()) : 0;
    int64_t epoch = o ? o->epoch_us() : s_epochEnv;
    return wall_clock(c) ? n + epoch : n;
}

/* ---- Threads: their wake, and the census ---- */

#define WAIT_FDS 8          /* the descriptors of one wait the census looks at */

struct thread_rec {
    int         efd;        /* the eventfd a wake writes to */
    int         wake;       /* the chip library's wake for this thread */
    atomic_int  blocked;
    int         cond_wake;
    pthread_cond_t* _Atomic cond;   /* the condition a timed wait is on */
    pthread_mutex_t* mutex;         /* and its mutex; under s_condLock */
    int         cond_fired;         /* its deadline came; under s_condLock */
    int         sem_wake;
    sem_t* _Atomic sem;             /* the semaphore it waits on */
    atomic_int  sem_fired;          /* a timed wait's deadline came */
    int         counted;    /* in the census */
    /* The descriptors it waits on while it is blocked (census_quiet): set
     * before it counts itself blocked, cleared after it counts itself
     * running, so whoever sees it blocked sees them. */
    struct pollfd waiting[WAIT_FDS];
    atomic_int  nwaiting;
};

static __thread struct thread_rec* t_rec;
static atomic_int s_live = 1;       /* threads in the census; the first one to begin with */
static atomic_int s_blocked;
static pthread_mutex_t s_condLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_key_t s_key;
static pthread_once_t s_keyOnce = PTHREAD_ONCE_INIT;

/* Every thread's record, for a signal to find the threads waiting on its
 * condition (cond_waiters_run); under s_condLock. */
#define MAX_RECS 1024
static struct thread_rec* s_recs[MAX_RECS];

static void recs_add(struct thread_rec* r)
{
    pthread_mutex_lock(&s_condLock);
    for (int i = 0; i < MAX_RECS; i++) {
        if (!s_recs[i]) {
            s_recs[i] = r;
            break;
        }
    }
    pthread_mutex_unlock(&s_condLock);
}

static void rec_gone(void* p)
{
    struct thread_rec* r = (struct thread_rec*)p;
    if (r) {
        pthread_mutex_lock(&s_condLock);
        for (int i = 0; i < MAX_RECS; i++)
            if (s_recs[i] == r) s_recs[i] = NULL;
        pthread_mutex_unlock(&s_condLock);
    }
    if (r && r->counted) {
        if (atomic_exchange(&r->blocked, 0)) atomic_fetch_sub(&s_blocked, 1);
        atomic_fetch_sub(&s_live, 1);
    }
}

static void make_key(void) { pthread_key_create(&s_key, rec_gone); }

static void wake_due(void* arg)
{
    struct thread_rec* r = (struct thread_rec*)arg;
    if (atomic_exchange(&r->blocked, 0)) atomic_fetch_sub(&s_blocked, 1);
    uint64_t one = 1;
    ssize_t w = REAL(write)(r->efd, &one, sizeof one);
    (void)w;
}

#define COND_TRY_NS 10000000        /* how long a deadline tries for the waiter's mutex, wall */

static int64_t real_mono_ns(void)
{
    struct timespec ts;
    REAL(clock_gettime)(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

/* A timed wait's deadline has come: its thread is running again, and the
 * condition is broadcast holding the waiter's mutex, as any signaller's is.
 * The waiter tells the census it is blocked, which is what lets T reach its
 * deadline, a moment before its wait gives the mutex up; a broadcast made in
 * between would find nobody waiting and be lost, and a thread that counts as
 * running and says nothing is what the busy watchdog lets T run away from.
 * The mutex is only tried, with s_condLock let go in between, since the
 * waiter takes s_condLock holding it; and only for COND_TRY_NS, after which
 * the broadcast goes without it rather than wait on a thread that holds it
 * and is itself waiting on this one, as a host that switches its threads by
 * signals can. `cond_fired` is what the waiter goes by (cond_node_wait). */
static void cond_due(void* arg)
{
    struct thread_rec* r = (struct thread_rec*)arg;
    if (atomic_exchange(&r->blocked, 0)) atomic_fetch_sub(&s_blocked, 1);
    sigset_t all, old;
    sigfillset(&all);
    int64_t give_up = real_mono_ns() + COND_TRY_NS;
    for (;;) {
        pthread_sigmask(SIG_BLOCK, &all, &old);
        pthread_mutex_lock(&s_condLock);
        pthread_cond_t* c = atomic_load(&r->cond);
        int done = 1;
        if (c && r->mutex) {            /* a timed wait: an untimed one has no mutex here */
            r->cond_fired = 1;
            if (pthread_mutex_trylock(r->mutex) == 0) {
                REAL(pthread_cond_broadcast)(c);
                pthread_mutex_unlock(r->mutex);
            } else if (real_mono_ns() >= give_up) {
                REAL(pthread_cond_broadcast)(c);
            } else {
                done = 0;
            }
        }
        pthread_mutex_unlock(&s_condLock);
        pthread_sigmask(SIG_SETMASK, &old, NULL);
        if (done) return;
        sched_yield();
    }
}

/* A timed semaphore wait's deadline has come. A post of its own would be a
 * token some other waiter could take, so it only says so; the waiter, which
 * looks every SEM_LOOK_NS of wall, sees it (sem_node_wait). */
static void sem_due(void* arg)
{
    struct thread_rec* r = (struct thread_rec*)arg;
    atomic_store(&r->sem_fired, 1);
    if (atomic_exchange(&r->blocked, 0)) atomic_fetch_sub(&s_blocked, 1);
}

static struct thread_rec* rec(const struct simclock_ops* o)
{
    if (t_rec) return t_rec;
    struct thread_rec* r = (struct thread_rec*)calloc(1, sizeof *r);
    if (!r) return NULL;
    r->efd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    r->wake = o->wake_create(wake_due, r);
    r->cond_wake = o->wake_create(cond_due, r);
    r->sem_wake = o->wake_create(sem_due, r);
    /* The process's first thread is in the census from the start; every other
     * one arrives through pthread_create. */
    r->counted = gettid() == getpid();
    pthread_once(&s_keyOnce, make_key);
    pthread_setspecific(s_key, r);
    t_rec = r;
    recs_add(r);
    return r;
}

/* What a thread is about to block on, for census_quiet. */
static void wait_on(struct thread_rec* r, const struct pollfd* fds, nfds_t n)
{
    if (!r) return;
    int k = 0;
    for (nfds_t i = 0; i < n && k < WAIT_FDS; i++) {
        if (fds[i].fd < 0) continue;
        r->waiting[k].fd = fds[i].fd;
        r->waiting[k].events = fds[i].events;
        r->waiting[k].revents = 0;
        k++;
    }
    atomic_store(&r->nwaiting, k);
}

static void wait_on_fd(struct thread_rec* r, int fd, short events)
{
    struct pollfd p = { fd, events, 0 };
    wait_on(r, &p, 1);
}

static void wait_done(struct thread_rec* r)
{
    if (r) atomic_store(&r->nwaiting, 0);
}

/* Whether nothing is on its way to a blocked thread: none of the descriptors
 * the blocked threads wait on is ready for them. A thread woken through a
 * descriptor - a pipe another thread of the station wrote, a socket - counts
 * as blocked until it runs and says otherwise, and a station that said idle
 * in between had T move under the work it was woken for; which of the two
 * the host ran first decided it. */
static int census_quiet(void)
{
    struct pollfd all[64];
    int n = 0;
    sigset_t sigs, old;
    sigfillset(&sigs);
    pthread_sigmask(SIG_BLOCK, &sigs, &old);
    pthread_mutex_lock(&s_condLock);
    for (int i = 0; i < MAX_RECS && n < 64; i++) {
        struct thread_rec* w = s_recs[i];
        if (!w || !atomic_load(&w->blocked)) continue;
        int k = atomic_load(&w->nwaiting);
        for (int j = 0; j < k && n < 64; j++) all[n++] = w->waiting[j];
    }
    pthread_mutex_unlock(&s_condLock);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    if (n == 0) return 1;
    for (int i = 0; i < n; i++) all[i].revents = 0;
    return REAL(poll)(all, (nfds_t)n, 0) <= 0;
}

/* The census: the station is idle when every thread it counts is blocked and
 * nothing is ready to wake one of them. A station not yet idle for the second
 * reason says so when the thread that was woken blocks again. */
static void census_block(const struct simclock_ops* o, struct thread_rec* r)
{
    if (!s_census || !o || !r || !r->counted) return;
    if (!atomic_exchange(&r->blocked, 1)) {
        int b = atomic_fetch_add(&s_blocked, 1) + 1;
        if (b >= atomic_load(&s_live) && census_quiet()) o->idle();
    }
}

static void census_run(struct thread_rec* r)
{
    if (!s_census || !r || !r->counted) return;
    if (atomic_exchange(&r->blocked, 0)) atomic_fetch_sub(&s_blocked, 1);
}

static void drain(int efd)
{
    uint64_t n;
    while (REAL(read)(efd, &n, sizeof n) == (ssize_t)sizeof n) {}
}

struct start {
    void* (*fn)(void*);
    void* arg;
};

static void* trampoline(void* p)
{
    struct start s = *(struct start*)p;
    free(p);
    pthread_once(&s_keyOnce, make_key);
    struct thread_rec* r = (struct thread_rec*)calloc(1, sizeof *r);
    if (r) {
        r->efd = -1;
        r->wake = -1;
        r->cond_wake = -1;
        r->sem_wake = -1;
        r->counted = 1;
        pthread_setspecific(s_key, r);
        t_rec = r;
        recs_add(r);
    }
    return s.fn(s.arg);
}

int pthread_create(pthread_t* th, const pthread_attr_t* attr, void* (*fn)(void*), void* arg)
{
    load_mode();
    if (!s_virtual || !s_census) return REAL(pthread_create)(th, attr, fn, arg);
    struct start* s = (struct start*)malloc(sizeof *s);
    if (!s) return EAGAIN;
    s->fn = fn;
    s->arg = arg;
    atomic_fetch_add(&s_live, 1);
    int rc = REAL(pthread_create)(th, attr, trampoline, s);
    if (rc != 0) {
        atomic_fetch_sub(&s_live, 1);
        free(s);
    }
    return rc;
}

/* A thread made by trampoline has a record without a wake; give it one. */
static struct thread_rec* ready_rec(const struct simclock_ops* o)
{
    struct thread_rec* r = t_rec;
    if (!r) return rec(o);
    if (r->efd < 0) {
        r->efd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
        r->wake = o->wake_create(wake_due, r);
        r->cond_wake = o->wake_create(cond_due, r);
        r->sem_wake = o->wake_create(sem_due, r);
    }
    return r;
}

/* A wait with no timeout has nothing to do with the clock: the C library's
 * own, counted in the census as a blocked thread, waiting on `wfds`. */
#define UNTIMED_CALL_ON(call, wfds, wn)                               \
    do {                                                              \
        if (!s_census) return call;                                   \
        struct thread_rec* r_ = ready_rec(o);                         \
        wait_on(r_, wfds, wn);                                        \
        census_block(o, r_);                                          \
        __typeof__(call) rc_ = call;                                  \
        int e_ = errno;                                               \
        census_run(r_);                                               \
        wait_done(r_);                                                \
        errno = e_;                                                   \
        return rc_;                                                   \
    } while (0)
#define UNTIMED_CALL(call) UNTIMED_CALL_ON(call, NULL, 0)

/* Wait on this thread's eventfd, and `extra` descriptors, until one is ready,
 * a signal arrives, or node time reaches `deadline` (µs, NEVER for none).
 * Returns what the real ppoll returned over the extra descriptors (-1 EINTR
 * on a signal), or 0 when the deadline came first. */
static int node_wait(const struct simclock_ops* o, struct pollfd* extra, nfds_t n,
                     int64_t deadline, const sigset_t* mask)
{
    struct thread_rec* r = ready_rec(o);
    if (!r || r->efd < 0) return -2;
    deadline = wait_end(o, deadline);
    struct pollfd local[64];
    struct pollfd* fds = n + 1 <= 64 ? local : (struct pollfd*)malloc((n + 1) * sizeof *fds);
    if (!fds) return -2;
    if (n) memcpy(fds, extra, n * sizeof *fds);
    fds[n].fd = r->efd;
    fds[n].events = POLLIN;
    if (deadline != NEVER) o->wake_at(r->wake, deadline);
    int result = 0;
    for (;;) {
        if (deadline != NEVER && o->node_us() >= deadline) { result = 0; break; }
        wait_on(r, fds, n + 1);
        census_block(o, r);
        fds[n].revents = 0;
        int rc = REAL(ppoll)(fds, n + 1, NULL, mask);
        census_run(r);
        wait_done(r);
        if (rc < 0) { result = -1; break; }
        if (fds[n].revents) drain(r->efd);
        int ready = 0;
        for (nfds_t i = 0; i < n; i++) if (fds[i].revents) ready++;
        if (ready) {
            memcpy(extra, fds, n * sizeof *fds);
            result = ready;
            break;
        }
    }
    int saved = errno;
    if (deadline != NEVER) o->wake_at(r->wake, NEVER);
    if (fds != local) free(fds);
    errno = saved;
    return result;
}

/* ---- Clocks ---- */

static atomic_llong s_clockReads;

int clock_gettime(clockid_t c, struct timespec* ts)
{
    load_mode();
    if (!s_virtual || !(monotonic_clock(c) || wall_clock(c))) return REAL(clock_gettime)(c, ts);
    atomic_fetch_add(&s_clockReads, 1);
    us_ts(clock_now(ops(), c), ts);
    return 0;
}

int gettimeofday(struct timeval* tv, void* tz)
{
    load_mode();
    if (!s_virtual) return REAL(gettimeofday)(tv, tz);
    atomic_fetch_add(&s_clockReads, 1);
    us_tv(clock_now(ops(), CLOCK_REALTIME), tv);
    return 0;
}

time_t time(time_t* out)
{
    load_mode();
    if (!s_virtual) return REAL(time)(out);
    atomic_fetch_add(&s_clockReads, 1);
    time_t t = (time_t)(clock_now(ops(), CLOCK_REALTIME) / 1000000);
    if (out) *out = t;
    return t;
}

/* ---- Sleeps ---- */

static int sleep_until(const struct simclock_ops* o, int64_t deadline, int64_t* left)
{
    int rc = node_wait(o, NULL, 0, deadline, NULL);
    if (rc == -1) {
        if (left) {
            int64_t l = deadline - o->node_us();
            *left = l > 0 ? l : 0;
        }
        errno = EINTR;
        return -1;
    }
    return 0;
}

int nanosleep(const struct timespec* req, struct timespec* rem)
{
    const struct simclock_ops* o = ops();
    if (!o || !req) return REAL(nanosleep)(req, rem);
    int64_t left = 0;
    int rc = sleep_until(o, o->node_us() + ts_us(req), &left);
    if (rc < 0 && rem) us_ts(left, rem);
    return rc;
}

int clock_nanosleep(clockid_t c, int flags, const struct timespec* req, struct timespec* rem)
{
    const struct simclock_ops* o = ops();
    if (!o || !req || !(monotonic_clock(c) || wall_clock(c)))
        return REAL(clock_nanosleep)(c, flags, req, rem);
    int64_t deadline = (flags & TIMER_ABSTIME)
        ? ts_us(req) - (wall_clock(c) ? o->epoch_us() : 0)
        : o->node_us() + ts_us(req);
    int64_t left = 0;
    if (sleep_until(o, deadline, &left) < 0) {
        if (rem && !(flags & TIMER_ABSTIME)) us_ts(left, rem);
        return EINTR;
    }
    return 0;
}

int usleep(useconds_t us)
{
    const struct simclock_ops* o = ops();
    if (!o) return REAL(usleep)(us);
    return sleep_until(o, o->node_us() + us, NULL);
}

unsigned sleep(unsigned s)
{
    const struct simclock_ops* o = ops();
    if (!o) return REAL(sleep)(s);
    int64_t left = 0;
    if (sleep_until(o, o->node_us() + (int64_t)s * 1000000, &left) < 0)
        return (unsigned)((left + 999999) / 1000000);
    return 0;
}

/* ---- The interval timer ---- */

static struct itimerval s_itimer;
static int64_t s_itNext = NEVER;
static int s_itWake = -1;

static int64_t it_first(int64_t now, int64_t value, int64_t interval)
{
    int64_t at = now + value;
    if (interval > 0) at = ((at + interval - 1) / interval) * interval;   /* on its multiples */
    return at;
}

static void it_due(void* arg)
{
    (void)arg;
    const struct simclock_ops* o = atomic_load(&s_ops);
    int64_t interval = tv_us(&s_itimer.it_interval);
    if (interval > 0) {
        int64_t now = o->node_us();
        int64_t next = s_itNext + interval;
        while (next <= now) next += interval;
        s_itNext = next;
        o->wake_at(s_itWake, next);
    } else {
        s_itNext = NEVER;
    }
    kill(getpid(), SIGALRM);
}

static void it_arm(const struct simclock_ops* o)
{
    if (s_itWake < 0) s_itWake = o->wake_create(it_due, NULL);
    int64_t value = tv_us(&s_itimer.it_value);
    s_itNext = value > 0 ? it_first(o->node_us(), value, tv_us(&s_itimer.it_interval)) : NEVER;
    o->wake_at(s_itWake, s_itNext);
}

int setitimer(__itimer_which_t which, const struct itimerval* nv, struct itimerval* old)
{
    load_mode();
    if (!s_virtual || which != ITIMER_REAL) return REAL(setitimer)(which, nv, old);
    if (old) *old = s_itimer;
    if (nv) s_itimer = *nv;
    const struct simclock_ops* o = atomic_load(&s_ops);
    if (o) it_arm(o);
    return 0;
}

int getitimer(__itimer_which_t which, struct itimerval* cur)
{
    load_mode();
    if (!s_virtual || which != ITIMER_REAL) return REAL(getitimer)(which, cur);
    if (cur) {
        *cur = s_itimer;
        const struct simclock_ops* o = atomic_load(&s_ops);
        if (o && s_itNext != NEVER) {
            int64_t left = s_itNext - o->node_us();
            us_tv(left > 0 ? left : 0, &cur->it_value);
        }
    }
    return 0;
}

/* ---- Descriptor waits with a timeout ---- */

int ppoll(struct pollfd* fds, nfds_t n, const struct timespec* tmo, const sigset_t* mask)
{
    const struct simclock_ops* o = ops();
    if (!o || (tmo && tmo->tv_sec == 0 && tmo->tv_nsec == 0))
        return REAL(ppoll)(fds, n, tmo, mask);
    if (!tmo) UNTIMED_CALL_ON(REAL(ppoll)(fds, n, tmo, mask), fds, n);
    int64_t deadline = o->node_us() + ts_us(tmo);
    int rc = node_wait(o, fds, n, deadline, mask);
    if (rc == -2) return REAL(ppoll)(fds, n, tmo, mask);
    if (rc == 0) for (nfds_t i = 0; i < n; i++) fds[i].revents = 0;
    return rc;
}

int poll(struct pollfd* fds, nfds_t n, int timeout)
{
    const struct simclock_ops* o = ops();
    if (!o || timeout == 0) return REAL(poll)(fds, n, timeout);
    struct timespec ts;
    us_ts((int64_t)timeout * 1000, &ts);
    return ppoll(fds, n, timeout < 0 ? NULL : &ts, NULL);
}

static int sets_to_poll(int nfds, fd_set* r, fd_set* w, fd_set* e, struct pollfd* out, int cap)
{
    int n = 0;
    for (int fd = 0; fd < nfds && n < cap; fd++) {
        short ev = 0;
        if (r && FD_ISSET(fd, r)) ev |= POLLIN;
        if (w && FD_ISSET(fd, w)) ev |= POLLOUT;
        if (e && FD_ISSET(fd, e)) ev |= POLLPRI;
        if (!ev) continue;
        out[n].fd = fd;
        out[n].events = ev;
        out[n].revents = 0;
        n++;
    }
    return n;
}

static int pselect_node(int nfds, fd_set* r, fd_set* w, fd_set* e, int64_t deadline,
                        const sigset_t* mask, const struct simclock_ops* o)
{
    struct pollfd fds[FD_SETSIZE];
    int n = sets_to_poll(nfds, r, w, e, fds, FD_SETSIZE);
    int rc = node_wait(o, fds, (nfds_t)n, deadline, mask);
    if (rc < 0) return rc;
    if (r) FD_ZERO(r);
    if (w) FD_ZERO(w);
    if (e) FD_ZERO(e);
    int count = 0;
    for (int i = 0; i < n && rc > 0; i++) {
        short re = fds[i].revents;
        if (r && (re & (POLLIN | POLLHUP | POLLERR))) { FD_SET(fds[i].fd, r); count++; }
        if (w && (re & (POLLOUT | POLLERR))) { FD_SET(fds[i].fd, w); count++; }
        if (e && (re & POLLPRI)) { FD_SET(fds[i].fd, e); count++; }
    }
    return count;
}

int pselect(int nfds, fd_set* r, fd_set* w, fd_set* e, const struct timespec* tmo,
            const sigset_t* mask)
{
    const struct simclock_ops* o = ops();
    if (!o || (tmo && tmo->tv_sec == 0 && tmo->tv_nsec == 0))
        return REAL(pselect)(nfds, r, w, e, tmo, mask);
    if (!tmo) UNTIMED_CALL(REAL(pselect)(nfds, r, w, e, tmo, mask));
    int rc = pselect_node(nfds, r, w, e, o->node_us() + ts_us(tmo), mask, o);
    if (rc == -2) return REAL(pselect)(nfds, r, w, e, tmo, mask);
    return rc;
}

int select(int nfds, fd_set* r, fd_set* w, fd_set* e, struct timeval* tmo)
{
    const struct simclock_ops* o = ops();
    if (!o || (tmo && tmo->tv_sec == 0 && tmo->tv_usec == 0))
        return REAL(select)(nfds, r, w, e, tmo);
    if (!tmo) UNTIMED_CALL(REAL(select)(nfds, r, w, e, tmo));
    int rc = pselect_node(nfds, r, w, e, o->node_us() + tv_us(tmo), NULL, o);
    if (rc == -2) return REAL(select)(nfds, r, w, e, tmo);
    return rc;
}

int epoll_pwait(int epfd, struct epoll_event* ev, int max, int timeout, const sigset_t* mask)
{
    const struct simclock_ops* o = ops();
    if (!o || timeout == 0) return REAL(epoll_pwait)(epfd, ev, max, timeout, mask);
    if (timeout < 0) {
        struct pollfd ep = { epfd, POLLIN, 0 };
        UNTIMED_CALL_ON(REAL(epoll_pwait)(epfd, ev, max, timeout, mask), &ep, 1);
    }
    int64_t deadline = o->node_us() + (int64_t)timeout * 1000;
    for (;;) {
        struct pollfd p = { epfd, POLLIN, 0 };
        int rc = node_wait(o, &p, 1, deadline, mask);
        if (rc == -2) return REAL(epoll_pwait)(epfd, ev, max, timeout, mask);
        if (rc <= 0) return rc;
        rc = REAL(epoll_wait)(epfd, ev, max, 0);
        if (rc != 0) return rc;
    }
}

int epoll_wait(int epfd, struct epoll_event* ev, int max, int timeout)
{
    return epoll_pwait(epfd, ev, max, timeout, NULL);
}

/* ---- Condition variables ---- */

/* A timed wait in node time: the C library's untimed wait on the condition,
 * ended by a signal or broadcast of the host's or by the thread's wake at the
 * deadline (cond_due), which says ETIMEDOUT as the real one does. */
static int cond_node_wait(const struct simclock_ops* o, pthread_cond_t* c, pthread_mutex_t* m,
                          int64_t deadline)
{
    struct thread_rec* r = ready_rec(o);
    if (!r || r->cond_wake < 0) return -2;
    if (o->node_us() >= deadline) return ETIMEDOUT;
    sigset_t all, old;
    sigfillset(&all);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    pthread_mutex_lock(&s_condLock);
    r->mutex = m;
    r->cond_fired = 0;
    atomic_store(&r->cond, c);
    pthread_mutex_unlock(&s_condLock);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    deadline = resolve(deadline);
    o->wake_at(r->cond_wake, deadline);
    census_block(o, r);
    int rc = REAL(pthread_cond_wait)(c, m);
    census_run(r);
    o->wake_at(r->cond_wake, NEVER);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    pthread_mutex_lock(&s_condLock);
    atomic_store(&r->cond, NULL);
    r->mutex = NULL;
    if (rc == 0 && r->cond_fired) rc = ETIMEDOUT;
    pthread_mutex_unlock(&s_condLock);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    return rc;
}

/* A timedwait's deadline on the clock its condition keeps, as node time: the
 * wall clock's, or the monotonic one's for a condition made with
 * pthread_condattr_setclock, whose deadlines fall far below the run's epoch. */
static int64_t cond_deadline(const struct simclock_ops* o, const struct timespec* abst)
{
    int64_t at = ts_us(abst);
    int64_t epoch = o->epoch_us();
    return at < epoch / 2 ? at : at - epoch;
}

int pthread_cond_timedwait(pthread_cond_t* c, pthread_mutex_t* m, const struct timespec* abst)
{
    const struct simclock_ops* o = ops();
    if (!o) return REAL(pthread_cond_timedwait)(c, m, abst);
    int rc = cond_node_wait(o, c, m, cond_deadline(o, abst));
    return rc == -2 ? REAL(pthread_cond_timedwait)(c, m, abst) : rc;
}

int pthread_cond_clockwait(pthread_cond_t* c, pthread_mutex_t* m, clockid_t clk,
                           const struct timespec* abst)
{
    const struct simclock_ops* o = ops();
    if (!o || !(monotonic_clock(clk) || wall_clock(clk)))
        return REAL(pthread_cond_clockwait)(c, m, clk, abst);
    int rc = cond_node_wait(o, c, m, ts_us(abst) - (wall_clock(clk) ? o->epoch_us() : 0));
    return rc == -2 ? REAL(pthread_cond_clockwait)(c, m, clk, abst) : rc;
}

int pthread_cond_wait(pthread_cond_t* c, pthread_mutex_t* m)
{
    const struct simclock_ops* o = ops();
    if (!o || !s_census) return REAL(pthread_cond_wait)(c, m);
    struct thread_rec* r = ready_rec(o);
    if (!r) return REAL(pthread_cond_wait)(c, m);
    sigset_t all, old;
    sigfillset(&all);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    pthread_mutex_lock(&s_condLock);
    atomic_store(&r->cond, c);
    pthread_mutex_unlock(&s_condLock);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    census_block(o, r);
    int rc = REAL(pthread_cond_wait)(c, m);
    census_run(r);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    pthread_mutex_lock(&s_condLock);
    atomic_store(&r->cond, NULL);
    pthread_mutex_unlock(&s_condLock);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    return rc;
}

/* A thread waiting on `c` is about to be woken: it counts as running from
 * now, not from when it next gets the CPU. Otherwise the signaller blocking
 * next would find every thread blocked and say the station is idle while the
 * one it woke has work to do, and T would move under it. A signal wakes one
 * waiter the C library chooses, so every waiter on `c` counts as running; one
 * not woken is counted blocked again the next time it waits, and until then
 * the busy watchdog stands in for its idle. */
static void cond_waiters_run(pthread_cond_t* c)
{
    if (!s_census || !ops()) return;
    sigset_t all, old;
    sigfillset(&all);
    pthread_sigmask(SIG_BLOCK, &all, &old);
    pthread_mutex_lock(&s_condLock);
    for (int i = 0; i < MAX_RECS; i++) {
        struct thread_rec* r = s_recs[i];
        if (r && atomic_load(&r->cond) == c && atomic_exchange(&r->blocked, 0))
            atomic_fetch_sub(&s_blocked, 1);
    }
    pthread_mutex_unlock(&s_condLock);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
}

int pthread_cond_signal(pthread_cond_t* c)
{
    cond_waiters_run(c);
    return REAL(pthread_cond_signal)(c);
}

int pthread_cond_broadcast(pthread_cond_t* c)
{
    cond_waiters_run(c);
    return REAL(pthread_cond_broadcast)(c);
}

/* ---- Semaphores ---- */

static atomic_int s_semWaiting;     /* threads inside a semaphore wait */

#define SEM_LOOK_NS (2 * 1000 * 1000)   /* wall between a timed wait's looks at its deadline */

static void sem_enter(struct thread_rec* r, sem_t* s)
{
    atomic_store(&r->sem, s);
    atomic_fetch_add(&s_semWaiting, 1);
}

static void sem_leave(struct thread_rec* r)
{
    atomic_store(&r->sem, NULL);
    atomic_fetch_sub(&s_semWaiting, 1);
}

/* A timed wait in node time. The C library's own wait, in slices of
 * SEM_LOOK_NS of wall, ended by a post, a signal, or the thread's wake at the
 * deadline (sem_due), which says ETIMEDOUT as the real one does. The thread
 * counts as blocked across the slices: they are the shim looking, not the
 * station working. */
static int sem_node_wait(const struct simclock_ops* o, sem_t* s, int64_t deadline)
{
    struct thread_rec* r = ready_rec(o);
    if (!r || r->sem_wake < 0) return -2;
    if (REAL(sem_trywait)(s) == 0) return 0;
    if (errno != EAGAIN) return -1;
    if (o->node_us() >= deadline) {
        errno = ETIMEDOUT;
        return -1;
    }
    atomic_store(&r->sem_fired, 0);
    sem_enter(r, s);
    o->wake_at(r->sem_wake, resolve(deadline));
    int rc, err = 0;
    for (;;) {
        census_block(o, r);
        struct timespec until;
        REAL(clock_gettime)(CLOCK_MONOTONIC, &until);
        until.tv_nsec += SEM_LOOK_NS;
        if (until.tv_nsec >= 1000000000) {
            until.tv_sec += 1;
            until.tv_nsec -= 1000000000;
        }
        rc = REAL(sem_clockwait)(s, CLOCK_MONOTONIC, &until);
        if (rc == 0) break;
        err = errno;
        if (err != ETIMEDOUT || atomic_load(&r->sem_fired)) break;
    }
    census_run(r);
    o->wake_at(r->sem_wake, NEVER);
    sem_leave(r);
    if (rc != 0) errno = err;
    return rc;
}

int sem_wait(sem_t* s)
{
    const struct simclock_ops* o = ops();
    if (!o || !s_census) return REAL(sem_wait)(s);
    struct thread_rec* r = ready_rec(o);
    if (!r) return REAL(sem_wait)(s);
    sem_enter(r, s);
    census_block(o, r);
    int rc = REAL(sem_wait)(s);
    int e = errno;
    census_run(r);
    sem_leave(r);
    errno = e;
    return rc;
}

int sem_timedwait(sem_t* s, const struct timespec* abst)
{
    const struct simclock_ops* o = ops();
    if (!o) return REAL(sem_timedwait)(s, abst);
    int rc = sem_node_wait(o, s, ts_us(abst) - o->epoch_us());
    return rc == -2 ? REAL(sem_timedwait)(s, abst) : rc;
}

int sem_clockwait(sem_t* s, clockid_t clk, const struct timespec* abst)
{
    const struct simclock_ops* o = ops();
    if (!o || !(monotonic_clock(clk) || wall_clock(clk)))
        return REAL(sem_clockwait)(s, clk, abst);
    int rc = sem_node_wait(o, s, ts_us(abst) - (wall_clock(clk) ? o->epoch_us() : 0));
    return rc == -2 ? REAL(sem_clockwait)(s, clk, abst) : rc;
}

/* A post wakes one waiter the C library chooses, so every thread waiting on
 * the semaphore counts as running from now, as for a condition's signal. */
int sem_post(sem_t* s)
{
    if (s_census && atomic_load(&s_semWaiting) > 0 && ops()) {
        sigset_t all, old;
        sigfillset(&all);
        pthread_sigmask(SIG_BLOCK, &all, &old);
        pthread_mutex_lock(&s_condLock);
        for (int i = 0; i < MAX_RECS; i++) {
            struct thread_rec* r = s_recs[i];
            if (r && atomic_load(&r->sem) == s && atomic_exchange(&r->blocked, 0))
                atomic_fetch_sub(&s_blocked, 1);
        }
        pthread_mutex_unlock(&s_condLock);
        pthread_sigmask(SIG_SETMASK, &old, NULL);
    }
    return REAL(sem_post)(s);
}

/* ---- Blocking reads, for the census ---- */

/* Whether a read on `fd` can wait on something outside the process. A
 * regular file, a directory or a disk never makes it wait, however long the
 * read takes: a thread reading one is working, and counted blocked it could
 * make the station look idle with its work half done. */
static int blocking(int fd, int flags)
{
    if (flags & MSG_DONTWAIT) return 0;
    int fl = fcntl(fd, F_GETFL);
    if (fl < 0 || (fl & O_NONBLOCK)) return 0;
    struct stat st;
    if (fstat(fd, &st) == 0 && (S_ISREG(st.st_mode) || S_ISDIR(st.st_mode) || S_ISBLK(st.st_mode)))
        return 0;
    return 1;
}

/* `rc = call`, the thread counted as blocked around it when it can block. */
#define CENSUS_RUN(rc, call, fd, flags)                                   \
    do {                                                                  \
        const struct simclock_ops* o_ = ops();                            \
        if (!o_ || !s_census || !blocking(fd, flags)) { rc = call; break; } \
        struct thread_rec* r_ = ready_rec(o_);                            \
        wait_on_fd(r_, fd, POLLIN);                                       \
        census_block(o_, r_);                                             \
        rc = call;                                                        \
        int e_ = errno;                                                   \
        census_run(r_);                                                   \
        wait_done(r_);                                                    \
        errno = e_;                                                       \
    } while (0)

/* ---- Bytes from outside the air, for the ether ---- */

/* The chip library's socket to the ether, learned from the hello it sends on
 * it: a report goes out behind everything the station has sent before it and
 * ahead of the idle that follows. */
static atomic_int s_linkFd = -1;
static atomic_llong s_ttyIn;        /* bytes read from the console */
static atomic_llong s_ioCalls;      /* reads and writes the station has made */

#define TCP_KEY 112                 /* "tcp/" and two "addr:port" */

static void report(const char* line, int len)
{
    int fd = atomic_load(&s_linkFd);
    if (fd < 0 || len <= 0) return;
    ssize_t w = REAL(send)(fd, line, (size_t)len, MSG_DONTWAIT);
    (void)w;
}

static void report_tty(const char* type, long long total)
{
    char line[128];
    report(line, snprintf(line, sizeof line,
                          "{\"type\":\"%s\",\"sid\":%d,\"ch\":\"tty\",\"total\":%lld}",
                          type, s_sid, total));
}

static void report_tcp(const char* type, const char* key, long long n)
{
    char line[TCP_KEY + 96];
    report(line, snprintf(line, sizeof line,
                          "{\"type\":\"%s\",\"sid\":%d,\"ch\":\"%s\",\"n\":%lld}",
                          type, s_sid, key, n));
}

/* The channel a descriptor is, when it is an IPv4 TCP connection: "tcp/", the
 * writer's end and the reader's. 0 for anything else. */
static int tcp_key(int fd, int writing, char* key)
{
    int type = 0;
    socklen_t tl = sizeof type;
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &tl) != 0 || type != SOCK_STREAM) return 0;
    struct sockaddr_in me, peer;
    socklen_t ml = sizeof me, pl = sizeof peer;
    if (getsockname(fd, (struct sockaddr*)&me, &ml) != 0 || me.sin_family != AF_INET) return 0;
    if (getpeername(fd, (struct sockaddr*)&peer, &pl) != 0 || peer.sin_family != AF_INET) return 0;
    char a[INET_ADDRSTRLEN], b[INET_ADDRSTRLEN];
    inet_ntop(AF_INET, &me.sin_addr, a, sizeof a);
    inet_ntop(AF_INET, &peer.sin_addr, b, sizeof b);
    if (writing)
        snprintf(key, TCP_KEY, "tcp/%s:%u>%s:%u", a, ntohs(me.sin_port), b, ntohs(peer.sin_port));
    else
        snprintf(key, TCP_KEY, "tcp/%s:%u>%s:%u", b, ntohs(peer.sin_port), a, ntohs(me.sin_port));
    return 1;
}

static int reporting(int fd)
{
    load_mode();
    return s_virtual && atomic_load(&s_linkFd) >= 0 && fd != atomic_load(&s_linkFd);
}

/* After a read of `rc` bytes from `fd`. The console's count goes out when the
 * read leaves nothing waiting, so a reader taking a byte at a time reports
 * once per burst. */
static void took(int fd, ssize_t rc, int flags)
{
    atomic_fetch_add(&s_ioCalls, 1);
    if (rc <= 0 || (flags & MSG_PEEK)) return;
    load_mode();
    if (!s_virtual) return;
    int saved = errno, said = 0;
    if (fd == 0) {
        long long total = atomic_fetch_add(&s_ttyIn, rc) + rc;
        int left = 0;
        if (ioctl(0, FIONREAD, &left) != 0 || left <= 0) {
            report_tty("read", total);
            said = 1;
        }
    } else if (reporting(fd)) {
        char key[TCP_KEY];
        if (tcp_key(fd, 0, key)) {
            report_tcp("read", key, rc);
            said = 1;
        }
    }
    /* What it read is input the station is at work on now: it owes an idle
     * for it, as for anything the ether tells it, and the ether, which takes
     * the report as the station speaking, waits for that idle. Sending it a
     * message for the purpose instead would land it at the host's moment, in
     * the middle of that work. */
    const struct simclock_ops* o = said ? ops() : NULL;
    if (o && o->spoke) o->spoke();
    errno = saved;
}

/* A writing thread's own socket to the ether, on which it asks to write and
 * hears that it may. */
static __thread int t_goFd = -1;

#define GO_WAIT_MS 1000             /* an ether that does not answer is not waited for */

static int go_socket(void)
{
    if (t_goFd >= 0) return t_goFd;
    const char* e = getenv("SIM_MESH_ETHER");
    const char* colon = e ? strrchr(e, ':') : NULL;
    char host[INET_ADDRSTRLEN];
    if (!colon || (size_t)(colon - e) >= sizeof host) return -1;
    memcpy(host, e, (size_t)(colon - e));
    host[colon - e] = '\0';
    struct sockaddr_in to = { 0 };
    to.sin_family = AF_INET;
    to.sin_port = htons((uint16_t)atoi(colon + 1));
    if (inet_pton(AF_INET, host, &to.sin_addr) != 1) return -1;
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    if (REAL(connect)(fd, (struct sockaddr*)&to, sizeof to) != 0) {
        close(fd);
        return -1;
    }
    t_goFd = fd;
    return fd;
}

/* `n` bytes are about to go out on the TCP connection `key`: the ether is
 * told, and the write waits for its go-ahead, which comes once the reader has
 * the run's T and nothing else in hand. The reader then reads them at that T
 * however the two stations' threads fall on the host. */
static void ask_to_write(const char* key, size_t n)
{
    static atomic_uint s_req;
    int g = go_socket();
    if (g < 0) {
        report_tcp("wrote", key, (long long)n);
        return;
    }
    unsigned req = atomic_fetch_add(&s_req, 1) + 1;
    char line[TCP_KEY + 128];
    int len = snprintf(line, sizeof line,
                       "{\"type\":\"wrote\",\"sid\":%d,\"ch\":\"%s\",\"n\":%lld,\"go\":%u}",
                       s_sid, key, (long long)n, req);
    if (REAL(send)(g, line, (size_t)len, 0) != len) return;
    char want[32];
    snprintf(want, sizeof want, "\"go\":%u}", req);
    struct timespec now, end;
    REAL(clock_gettime)(CLOCK_MONOTONIC, &end);
    end.tv_sec += GO_WAIT_MS / 1000;
    for (;;) {
        REAL(clock_gettime)(CLOCK_MONOTONIC, &now);
        int64_t left = ((int64_t)end.tv_sec - now.tv_sec) * 1000000000LL + (end.tv_nsec - now.tv_nsec);
        if (left <= 0) return;
        struct timespec tmo = { (time_t)(left / 1000000000LL), (long)(left % 1000000000LL) };
        struct pollfd p = { g, POLLIN, 0 };
        if (REAL(ppoll)(&p, 1, &tmo, NULL) <= 0) continue;
        char buf[128];
        ssize_t got = REAL(recv)(g, buf, sizeof buf - 1, MSG_DONTWAIT);
        if (got <= 0) continue;
        buf[got] = '\0';
        if (strstr(buf, want)) return;
    }
}

/* Before a write of `n` bytes to `fd`: a TCP connection's are announced now,
 * while the bytes do not yet exist. Returns whether they were. */
static int announce(int fd, size_t n, char* key)
{
    atomic_fetch_add(&s_ioCalls, 1);
    if (fd <= 2 || n == 0 || !reporting(fd)) return 0;
    int saved = errno;
    int yes = tcp_key(fd, 1, key);
    if (yes) ask_to_write(key, n);
    errno = saved;
    return yes;
}

/* After an announced write, `rc` of the `n`: what the call did not take is
 * taken back. */
static void wrote(size_t n, ssize_t rc, int announced, const char* key)
{
    size_t done = rc > 0 ? (size_t)rc : 0;
    if (!announced || done >= n) return;
    int saved = errno;
    report_tcp("wrote", key, (long long)done - (long long)n);
    errno = saved;
}

static size_t iov_len(const struct iovec* v, int n)
{
    size_t len = 0;
    for (int i = 0; i < n; i++) len += v[i].iov_len;
    return len;
}

/* ---- The busy watchdog, while the station computes ----
 *
 * The chip library's busy watchdog reads a timerfd and, when it expires,
 * says the station is idle whatever its threads are doing, so that a thread
 * waiting where nothing can see it, or spinning until T moves, does not stop
 * T. A thread that is working is neither, and T moving under it would land
 * the rest of its work at a T that depends on the host's speed. So while
 * another thread of the process is on the CPU, the expiry is not handed to
 * the watchdog: the timer is set again and the watchdog waits on. Work that
 * takes longer than 20 ms of the host's time then takes no time of T's, the
 * same in every run. A spin is told from work after WATCH_SPIN_NS of looking
 * at it: its threads spend their time in the kernel (a task yielding in a
 * loop, on a host whose tasks switch by signals, is signal-mask calls) and
 * read and write nothing; computing is user time, and working reads or
 * writes. A spin is let through then, and anything after WATCH_CAP_NS.
 * Nothing here allocates: another thread may be switched out holding the
 * allocator's lock. */

#define WATCH_RECHECK_NS (5 * 1000 * 1000)
#define WATCH_SPIN_NS    (50LL * 1000 * 1000)
#define WATCH_CAP_NS     (10LL * 1000 * 1000 * 1000)
#define WATCH_SPIN_CLOCK_READS 1000

static _Atomic signed char s_fdKind[1024];     /* 1: a timerfd, -1: not, 0: unknown */

static int is_timerfd(int fd)
{
    if (fd < 0 || fd >= 1024) return 0;
    signed char k = atomic_load(&s_fdKind[fd]);
    if (k) return k > 0;
    char path[32], target[64];
    snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
    ssize_t len = readlink(path, target, sizeof target - 1);
    int yes = len > 0 && (target[len] = '\0', strcmp(target, "anon_inode:[timerfd]") == 0);
    atomic_store(&s_fdKind[fd], yes ? 1 : -1);
    return yes;
}

/* What the process's other threads are doing: WORKING_CPU when one is on the
 * CPU or waiting for it, WORKING_DISK when one waits on the disk (state D: a
 * read or write it started, which ends on its own); and the user and system
 * time, in clock ticks, of all of them. */
#define WORKING_CPU  1
#define WORKING_DISK 2

static int others_running(long long* user, long long* sys)
{
    *user = *sys = 0;
    int dir = open("/proc/self/task", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dir < 0) return 0;
    pid_t self = gettid();
    int running = 0;
    char buf[4096];
    for (;;) {
        long got = REAL(syscall)(SYS_getdents64, dir, buf, sizeof buf, 0, 0, 0);
        if (got <= 0) break;
        for (long at = 0; at < got;) {
            struct { uint64_t ino; int64_t off; unsigned short reclen; unsigned char type; char name[]; }* e =
                (void*)(buf + at);
            at += e->reclen;
            if (e->name[0] < '0' || e->name[0] > '9' || atoi(e->name) == self) continue;
            char path[64], stat[512];
            snprintf(path, sizeof path, "/proc/self/task/%s/stat", e->name);
            int f = open(path, O_RDONLY | O_CLOEXEC);
            if (f < 0) continue;
            ssize_t n = REAL(read)(f, stat, sizeof stat - 1);
            close(f);
            if (n <= 0) continue;
            stat[n] = '\0';
            /* "tid (name) S ppid … " — the state, ten fields, then utime and
             * stime; the name may hold anything, so from its last ')'. */
            char* p = strrchr(stat, ')');
            if (!p || p[1] != ' ') continue;
            if (p[2] == 'R') running |= WORKING_CPU;
            if (p[2] == 'D') running |= WORKING_DISK;
            p += 3;
            for (int field = 0; field < 10 && p; field++) p = strchr(p + 1, ' ');
            if (!p) continue;
            char* end;
            *user += strtoll(p + 1, &end, 10);
            *sys += strtoll(end, NULL, 10);
        }
    }
    close(dir);
    return running;
}

static int64_t mono_ns(void)
{
    struct timespec ts;
    REAL(clock_gettime)(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* The timer `fd` has expired and `buf` holds the count: hand it on once no
 * other thread is computing or waiting on the disk. A thread in a disk wait
 * is in the middle of work: let go, T would move under it, and it would
 * carry on at an instant that depends on the disk. Nor is it a spin, which
 * is told by time spent and nothing read or written: its write is under way,
 * and takes no CPU while it waits. */
static ssize_t hold_expiry(int fd, void* buf)
{
    int64_t start = mono_ns();
    long long user0, sys0, user, sys;
    long long io0 = atomic_load(&s_ioCalls);
    long long clocks0 = atomic_load(&s_clockReads);
    if (!others_running(&user0, &sys0)) return sizeof(uint64_t);
    for (;;) {
        struct itimerspec again = { { 0, 0 }, { 0, WATCH_RECHECK_NS } };
        if (timerfd_settime(fd, 0, &again, NULL) != 0) break;
        ssize_t rc = REAL(read)(fd, buf, sizeof(uint64_t));
        if (rc != (ssize_t)sizeof(uint64_t)) return rc;
        int working = others_running(&user, &sys);
        if (!working) break;
        int64_t spent = mono_ns() - start;
        if (spent >= WATCH_CAP_NS) break;
        if (working & WORKING_DISK) continue;
        if (spent >= WATCH_SPIN_NS && sys - sys0 >= user - user0
            && atomic_load(&s_ioCalls) == io0)
            break;
        if (spent >= WATCH_SPIN_NS
            && atomic_load(&s_clockReads) - clocks0 >= WATCH_SPIN_CLOCK_READS)
            break;
    }
    return sizeof(uint64_t);
}

ssize_t read(int fd, void* buf, size_t n)
{
    ssize_t rc;
    CENSUS_RUN(rc, REAL(read)(fd, buf, n), fd, 0);
    if (rc == (ssize_t)sizeof(uint64_t) && n == sizeof(uint64_t) && s_virtual && is_timerfd(fd)) {
        int saved = errno;
        rc = hold_expiry(fd, buf);
        errno = saved;
    }
    took(fd, rc, 0);
    return rc;
}

ssize_t readv(int fd, const struct iovec* v, int cnt)
{
    ssize_t rc;
    CENSUS_RUN(rc, REAL(readv)(fd, v, cnt), fd, 0);
    took(fd, rc, 0);
    return rc;
}

ssize_t recv(int fd, void* buf, size_t n, int fl)
{
    ssize_t rc;
    CENSUS_RUN(rc, REAL(recv)(fd, buf, n, fl), fd, fl);
    took(fd, rc, fl);
    return rc;
}

ssize_t recvfrom(int fd, void* buf, size_t n, int fl, struct sockaddr* a, socklen_t* al)
{
    ssize_t rc;
    CENSUS_RUN(rc, REAL(recvfrom)(fd, buf, n, fl, a, al), fd, fl);
    took(fd, rc, fl);
    return rc;
}

ssize_t recvmsg(int fd, struct msghdr* m, int fl)
{
    ssize_t rc;
    CENSUS_RUN(rc, REAL(recvmsg)(fd, m, fl), fd, fl);
    took(fd, rc, fl);
    return rc;
}

int accept(int fd, struct sockaddr* a, socklen_t* al)
{
    int rc;
    CENSUS_RUN(rc, REAL(accept)(fd, a, al), fd, 0);
    return rc;
}

int accept4(int fd, struct sockaddr* a, socklen_t* al, int fl)
{
    int rc;
    CENSUS_RUN(rc, REAL(accept4)(fd, a, al, fl), fd, 0);
    return rc;
}

/* A listening TCP socket is the station's end of whatever connects to it: the
 * ether is told, so a connection to it is known to be this station's before
 * the station has read from it, whichever process of the run shares its
 * address. */
int listen(int fd, int backlog)
{
    int rc = REAL(listen)(fd, backlog);
    if (rc != 0 || !reporting(fd)) return rc;
    int saved = errno;
    struct sockaddr_in me;
    socklen_t ml = sizeof me;
    if (getsockname(fd, (struct sockaddr*)&me, &ml) == 0 && me.sin_family == AF_INET) {
        char a[INET_ADDRSTRLEN], line[128];
        inet_ntop(AF_INET, &me.sin_addr, a, sizeof a);
        report(line, snprintf(line, sizeof line, "{\"type\":\"listen\",\"sid\":%d,\"at\":\"%s:%u\"}",
                              s_sid, a, ntohs(me.sin_port)));
    }
    errno = saved;
    return rc;
}

/* A TCP connection a station opens to a loopback address leaves from the
 * station's own (SIM_MESH_BIND_ADDR) when it has not been given one: every end
 * of a connection between two stations is then an address that says which
 * station it is, to the peer and to the ether. */
int connect(int fd, const struct sockaddr* a, socklen_t al)
{
    load_mode();
    const char* own = s_virtual ? getenv("SIM_MESH_BIND_ADDR") : NULL;
    if (own && *own && a && a->sa_family == AF_INET && al >= (socklen_t)sizeof(struct sockaddr_in)
        && (ntohl(((const struct sockaddr_in*)a)->sin_addr.s_addr) >> 24) == 127) {
        int type = 0;
        socklen_t tl = sizeof type;
        struct sockaddr_in me;
        socklen_t ml = sizeof me;
        if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &tl) == 0 && type == SOCK_STREAM
            && getsockname(fd, (struct sockaddr*)&me, &ml) == 0 && me.sin_family == AF_INET
            && me.sin_port == 0 && me.sin_addr.s_addr == htonl(INADDR_ANY)) {
            struct sockaddr_in bind_to = { 0 };
            bind_to.sin_family = AF_INET;
            if (inet_pton(AF_INET, own, &bind_to.sin_addr) == 1) {
                int saved = errno;
                if (bind(fd, (struct sockaddr*)&bind_to, sizeof bind_to) != 0) errno = saved;
            }
        }
    }
    return REAL(connect)(fd, a, al);
}

ssize_t write(int fd, const void* buf, size_t n)
{
    char key[TCP_KEY];
    int announced = announce(fd, n, key);
    ssize_t rc = REAL(write)(fd, buf, n);
    wrote(n, rc, announced, key);
    return rc;
}

ssize_t writev(int fd, const struct iovec* v, int cnt)
{
    char key[TCP_KEY];
    size_t n = iov_len(v, cnt);
    int announced = announce(fd, n, key);
    ssize_t rc = REAL(writev)(fd, v, cnt);
    wrote(n, rc, announced, key);
    return rc;
}

ssize_t send(int fd, const void* buf, size_t n, int fl)
{
    load_mode();
    if (s_virtual && n >= 15 && memcmp(buf, "{\"type\":\"hello\"", 15) == 0)
        atomic_store(&s_linkFd, fd);
    char key[TCP_KEY];
    int announced = announce(fd, n, key);
    ssize_t rc = REAL(send)(fd, buf, n, fl);
    wrote(n, rc, announced, key);
    return rc;
}

ssize_t sendto(int fd, const void* buf, size_t n, int fl, const struct sockaddr* a, socklen_t al)
{
    char key[TCP_KEY];
    int announced = announce(fd, n, key);
    ssize_t rc = REAL(sendto)(fd, buf, n, fl, a, al);
    wrote(n, rc, announced, key);
    return rc;
}

ssize_t sendmsg(int fd, const struct msghdr* m, int fl)
{
    char key[TCP_KEY];
    size_t n = m ? iov_len(m->msg_iov, (int)m->msg_iovlen) : 0;
    int announced = announce(fd, n, key);
    ssize_t rc = REAL(sendmsg)(fd, m, fl);
    wrote(n, rc, announced, key);
    return rc;
}

/* ---- Randomness ---- */

/* splitmix64 as a counter: draw n is a mix of key + n·γ. A call takes all the
 * words it needs in one atomic step, so its bytes do not depend on where
 * another thread's call falls, and no lock is held that a thread switched out
 * by a signal could keep. */
static atomic_uint_fast64_t s_draws;

static void fill(void* buf, size_t len)
{
    uint64_t n = atomic_fetch_add(&s_draws, (len + 7) / 8);
    unsigned char* b = (unsigned char*)buf;
    while (len) {
        uint64_t z = s_randKey + ++n * 0x9e3779b97f4a7c15ULL;
        z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
        z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
        z ^= z >> 31;
        size_t k = len < 8 ? len : 8;
        memcpy(b, &z, k);
        b += k;
        len -= k;
    }
}

int getentropy(void* buf, size_t len)
{
    load_mode();
    if (!s_seeded) return REAL(getentropy)(buf, len);
    if (len > 256) { errno = EIO; return -1; }
    fill(buf, len);
    return 0;
}

ssize_t getrandom(void* buf, size_t len, unsigned flags)
{
    load_mode();
    if (!s_seeded) return REAL(getrandom)(buf, len, flags);
    fill(buf, len);
    return (ssize_t)len;
}

/* Every other call number goes to the C library's syscall untouched. */
long syscall(long n, ...)
{
    va_list ap;
    va_start(ap, n);
    long a = va_arg(ap, long), b = va_arg(ap, long), c = va_arg(ap, long);
    long d = va_arg(ap, long), e = va_arg(ap, long), f = va_arg(ap, long);
    va_end(ap);
    load_mode();
    if (n == SYS_getrandom && s_seeded) {
        fill((void*)a, (size_t)b);
        return b;
    }
    return REAL(syscall)(n, a, b, c, d, e, f);
}

/* ---- The chip library arrives ---- */

void simclock_attach(const struct simclock_ops* o)
{
    load_mode();
    if (!s_virtual) return;
    atomic_store(&s_ops, o);
    if (tv_us(&s_itimer.it_value) > 0) it_arm(o);
}

/* Every C library function is looked up here, before main(), and never later:
 * dlsym takes the dynamic linker's lock, and on a host whose tasks are
 * switched by signals a task can be switched out holding it, leaving the next
 * task that looks something up blocked on it for good. */
__attribute__((constructor)) static void simclock_init(void)
{
    load_mode();
    (void)REAL(clock_gettime); (void)REAL(gettimeofday); (void)REAL(time);
    (void)REAL(nanosleep); (void)REAL(clock_nanosleep); (void)REAL(usleep);
    (void)REAL(sleep); (void)REAL(setitimer); (void)REAL(getitimer);
    (void)REAL(poll); (void)REAL(ppoll); (void)REAL(select); (void)REAL(pselect);
    (void)REAL(epoll_wait); (void)REAL(epoll_pwait);
    (void)REAL(pthread_cond_timedwait); (void)REAL(pthread_cond_clockwait);
    (void)REAL(pthread_cond_wait); (void)REAL(pthread_create);
    (void)REAL(read); (void)REAL(readv); (void)REAL(recv); (void)REAL(recvfrom);
    (void)REAL(recvmsg); (void)REAL(write); (void)REAL(writev); (void)REAL(send);
    (void)REAL(sendto); (void)REAL(sendmsg);
    (void)REAL(accept); (void)REAL(accept4); (void)REAL(connect);
    (void)REAL(getentropy); (void)REAL(getrandom); (void)REAL(syscall);
}
