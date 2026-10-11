/**
 * simclock — what the time shim needs of the station's clock.
 *
 * In a virtual-time run a station's process runs with the time shim
 * (libsimclock.so) preloaded, which answers the C library's clocks, sleeps
 * and timeouts in node time. The clock itself is the chip library's, which
 * finds the shim by name when it starts and hands it these.
 */
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct simclock_ops {
    int64_t (*node_us)(void);                               /* node time now, µs */
    int64_t (*epoch_us)(void);                              /* wall-clock µs at node time 0 */
    int     (*wake_create)(void (*due)(void*), void* arg);  /* a wake the caller owns */
    void    (*wake_at)(int wake, int64_t node_us);          /* INT64_MAX clears it */
    void    (*idle)(void);                                  /* nothing to do before the wakes */
    int64_t (*chip_next_us)(void);                          /* the chips' next timer, node µs;
                                                             * INT64_MAX with none */
    void    (*spoke)(void);                                 /* the station took input it is now
                                                             * at work on: it owes an idle */
    int64_t (*read_us)(void);                               /* node time as the station's code
                                                             * reads it: under strict time, a
                                                             * read may first wait for T to
                                                             * move (the conductor's readNowUs) */
};

/** The shim's entry point, looked up with dlsym(RTLD_DEFAULT). */
typedef void (*simclock_attach_fn)(const struct simclock_ops* ops);

#ifdef __cplusplus
}
#endif
