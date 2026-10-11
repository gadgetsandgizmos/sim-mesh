/*
 * A station that waits for time by reading the clock in a loop, for the time
 * shim's tests: from the welcome it reads CLOCK_MONOTONIC until 5.3 ms of node
 * time have gone by, prints
 *
 *     done <node us> <reads>   what the loop read last, and how many reads it took
 *
 * and then waits on nothing for good.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <sys/select.h>
#include <time.h>

#include "simradio.h"

static int64_t mono_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

int main(int argc, char** argv)
{
    if (argc < 2) return 2;
    if (simradio_station_open(3, "127.0.0.1", argv[1]) != 0) return 1;
    long reads = 0;
    int64_t now;
    do {
        now = mono_us();
        reads++;
    } while (now < 5300);
    printf("done %lld %ld\n", (long long)now, reads);
    fflush(stdout);
    select(0, NULL, NULL, NULL, NULL);
    return 0;
}
