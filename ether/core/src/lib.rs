//! The conductor of a virtual-time run, for ether.py's `CoreEther`.
//!
//! In a virtual-time run most of what the ether does is the barrier: a
//! station says `idle`, T moves to the next instant anything needs, and the
//! stations due there are sent a `run`. That is a few microseconds of work
//! done a few hundred thousand times a run, and in Python it was most of the
//! ether's time. Here it is done in Rust, on the ether's socket, in the event
//! loop's thread: the stations' conductor records (`seq`, `idle`, `until`,
//! `told`, the resend buffer), T, and the barrier's counts live here.
//!
//! Everything else stays in Python and is called at the same points as
//! before, synchronously, so a run takes the same steps in the same order:
//! the medium (`state` and `tx`, held for the barrier and handed over in
//! station order), the events on the ether's clock, what the stations printed,
//! a station's first words (`hello`), its channels (`wrote`, `read`), its host
//! door (`floor`), and pacing. This is the only conductor: the one in
//! ether.py's `Ether` is retired.

use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use serde_json::Value;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::ErrorKind;
use std::mem::ManuallyDrop;
use std::net::{IpAddr, SocketAddr, UdpSocket};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// Datagrams handled in one turn of the event loop, at most.
const PUMP_BATCH: usize = 256;

/// The largest datagram UDP carries.
const DATAGRAM_MAX: usize = 65_536;

/// A send the kernel cannot take at once is tried again this often, this
/// many times, as the event loop's transport would have buffered it.
const SEND_RETRY_MS: i32 = 100;
const SEND_RETRIES: usize = 50;

/// A pipe, both ends non-blocking and closed on exec: (read, write).
fn pipe() -> std::io::Result<(RawFd, RawFd)> {
    let mut fds = [0 as libc::c_int; 2];
    // SAFETY: two ints for the kernel to write.
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_NONBLOCK | libc::O_CLOEXEC) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((fds[0], fds[1]))
}

fn poke(fd: RawFd) {
    // SAFETY: one byte from a local; a full pipe already says what this would.
    unsafe { libc::write(fd, b"x".as_ptr().cast(), 1) };
}

fn empty(fd: RawFd) {
    let mut buf = [0u8; 64];
    // SAFETY: reading into a local buffer of the stated length.
    while unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) } > 0 {}
}

/// The datagrams the reader thread has taken off the ether's socket, in the
/// order they came, waiting for the loop's thread.
///
/// The socket's receive buffer is the kernel's, capped by net.core.rmem_max,
/// and a burst — a frame's end told to a hundred and seventy receivers, each
/// answering at once — fills it while the loop's thread is busy elsewhere. A
/// datagram the kernel drops then is a `state` or a `tx` the medium never
/// hears, and the run is not the run. So a thread of its own does nothing but
/// move datagrams from the socket to here, as fast as they come, and wakes the
/// loop (`wake`); the conductor handles them there, in the same order.
struct Inbox {
    queue: Mutex<VecDeque<(Vec<u8>, SocketAddr)>>,
    /// Datagrams the kernel has dropped on the socket's full buffer, as it
    /// said with the last one it handed over (SO_RXQ_OVFL).
    drops: AtomicU64,
    /// A byte is in `wake` for what is queued, or about to be.
    signaled: AtomicBool,
    stop: AtomicBool,
    /// Read end for the event loop, write end for the reader.
    wake: (RawFd, RawFd),
    /// Wakes the reader's poll to stop it.
    halt: (RawFd, RawFd),
    /// Times `signal` actually poked `wake` (the 0->1 edge), from either
    /// thread. Always on, like `drops`: one more atomic add on a path that
    /// already does one (Phase A instrumentation).
    wake_pokes: AtomicU64,
    /// Phase B: paired with `queue`'s own mutex (the standard pairing), so
    /// `pump` can wait for the next datagram without the wake pipe's round
    /// trip through the asyncio loop. The pipe stays, for when `pump` has
    /// given up and returned: see `pump`'s quiescence loop.
    cv: Condvar,
}

impl Inbox {
    fn signal(&self) {
        if !self.signaled.swap(true, Ordering::SeqCst) {
            poke(self.wake.1);
        }
        self.cv.notify_all();
    }

    /// The reader thread: the socket's datagrams into the queue, until stopped.
    fn read(self: Arc<Self>, fd: RawFd) {
        let on: libc::c_int = 1;
        // SAFETY: an int option on a socket the caller keeps open. Refused,
        // the count stays at nought, which the caller reads as none dropped.
        unsafe {
            libc::setsockopt(
                fd,
                libc::SOL_SOCKET,
                libc::SO_RXQ_OVFL,
                (&on as *const libc::c_int).cast(),
                std::mem::size_of::<libc::c_int>() as libc::socklen_t,
            )
        };
        let mut buf = vec![0u8; DATAGRAM_MAX];
        while !self.stop.load(Ordering::SeqCst) {
            let mut fds = [
                libc::pollfd { fd, events: libc::POLLIN, revents: 0 },
                libc::pollfd { fd: self.halt.0, events: libc::POLLIN, revents: 0 },
            ];
            // SAFETY: two valid pollfds; interrupted or not, the loop looks again.
            unsafe { libc::poll(fds.as_mut_ptr(), 2, -1) };
            let mut took = false;
            loop {
                match self.recv(fd, &mut buf) {
                    Ok((n, addr)) => {
                        self.queue
                            .lock()
                            .unwrap_or_else(|p| p.into_inner())
                            .push_back((buf[..n].to_vec(), addr));
                        took = true;
                    }
                    Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                    Err(_) => break,
                }
            }
            if took {
                self.signal();
            }
        }
    }

    /// One datagram, and the kernel's count of those it dropped on this
    /// socket so far, which comes with it as a control message.
    fn recv(&self, fd: RawFd, buf: &mut [u8]) -> std::io::Result<(usize, SocketAddr)> {
        // SAFETY: zeroed plain C structs, filled in by the kernel below.
        let mut from: libc::sockaddr_storage = unsafe { std::mem::zeroed() };
        let mut iov = libc::iovec { iov_base: buf.as_mut_ptr().cast(), iov_len: buf.len() };
        let mut control = [0u64; 8];
        let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
        msg.msg_name = (&mut from as *mut libc::sockaddr_storage).cast();
        msg.msg_namelen = std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t;
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = std::mem::size_of_val(&control) as _;
        // SAFETY: every pointer in `msg` is to a live local of the stated size.
        let n = unsafe { libc::recvmsg(fd, &mut msg, libc::MSG_DONTWAIT) };
        if n < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: walking the control messages the kernel wrote into `msg`.
        unsafe {
            let mut cmsg = libc::CMSG_FIRSTHDR(&msg);
            while !cmsg.is_null() {
                if (*cmsg).cmsg_level == libc::SOL_SOCKET && (*cmsg).cmsg_type == libc::SO_RXQ_OVFL {
                    let dropped = std::ptr::read_unaligned(libc::CMSG_DATA(cmsg) as *const u32);
                    self.drops.fetch_max(u64::from(dropped), Ordering::SeqCst);
                }
                cmsg = libc::CMSG_NXTHDR(&msg, cmsg);
            }
        }
        let addr = sockaddr_to(&from).ok_or_else(|| std::io::Error::other("not an IP address"))?;
        Ok((n as usize, addr))
    }
}

/// An IPv4 or IPv6 socket address the kernel wrote, as Rust's.
fn sockaddr_to(storage: &libc::sockaddr_storage) -> Option<SocketAddr> {
    match storage.ss_family as libc::c_int {
        libc::AF_INET => {
            // SAFETY: the family says this is a sockaddr_in.
            let sin = unsafe { &*(storage as *const libc::sockaddr_storage as *const libc::sockaddr_in) };
            let ip = std::net::Ipv4Addr::from(u32::from_be(sin.sin_addr.s_addr));
            Some(SocketAddr::new(IpAddr::V4(ip), u16::from_be(sin.sin_port)))
        }
        libc::AF_INET6 => {
            // SAFETY: the family says this is a sockaddr_in6.
            let sin6 = unsafe { &*(storage as *const libc::sockaddr_storage as *const libc::sockaddr_in6) };
            let ip = std::net::Ipv6Addr::from(sin6.sin6_addr.s6_addr);
            Some(SocketAddr::new(IpAddr::V6(ip), u16::from_be(sin6.sin6_port)))
        }
        _ => None,
    }
}

/// The reverse of `sockaddr_to`: a Rust address as `sendmmsg` wants it, with
/// the length of the variant actually written.
fn sockaddr_from(addr: SocketAddr) -> (libc::sockaddr_storage, libc::socklen_t) {
    // SAFETY: zeroed is a valid sockaddr_storage; only the prefix matching
    // the variant below is meaningful, and only that much is declared.
    let mut storage: libc::sockaddr_storage = unsafe { std::mem::zeroed() };
    let len = match addr {
        SocketAddr::V4(a) => {
            let sin = libc::sockaddr_in {
                sin_family: libc::AF_INET as _,
                sin_port: a.port().to_be(),
                sin_addr: libc::in_addr { s_addr: u32::from_ne_bytes(a.ip().octets()) },
                sin_zero: [0; 8],
            };
            // SAFETY: sockaddr_in is no larger than sockaddr_storage.
            unsafe { std::ptr::write((&mut storage as *mut libc::sockaddr_storage).cast(), sin) };
            std::mem::size_of::<libc::sockaddr_in>()
        }
        SocketAddr::V6(a) => {
            let sin6 = libc::sockaddr_in6 {
                sin6_family: libc::AF_INET6 as _,
                sin6_port: a.port().to_be(),
                sin6_flowinfo: 0,
                sin6_addr: libc::in6_addr { s6_addr: a.ip().octets() },
                sin6_scope_id: a.scope_id(),
            };
            // SAFETY: sockaddr_in6 is no larger than sockaddr_storage.
            unsafe { std::ptr::write((&mut storage as *mut libc::sockaddr_storage).cast(), sin6) };
            std::mem::size_of::<libc::sockaddr_in6>()
        }
    };
    (storage, len as libc::socklen_t)
}

/// time.monotonic(), which the event loop's clock is.
fn monotonic() -> f64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: a valid timespec to write into.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

/// An integer as Python's `isinstance(x, int)` takes it, a bool included.
fn as_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n.as_i64(),
        Value::Bool(b) => Some(*b as i64),
        _ => None,
    }
}

/// `int(x) if isinstance(x, (int, float)) else None`.
fn as_truncated(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().map(|u| u.min(i64::MAX as u64) as i64))
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Value::Bool(b) => Some(*b as i64),
        _ => None,
    }
}

fn addr_tuple(py: Python<'_>, addr: SocketAddr) -> PyObject {
    (addr.ip().to_string(), addr.port()).into_py(py)
}

fn parse_addr(addr: (String, u16)) -> PyResult<SocketAddr> {
    let ip: IpAddr = addr
        .0
        .parse()
        .map_err(|_| PyValueError::new_err(format!("not an address: {}", addr.0)))?;
    Ok(SocketAddr::new(ip, addr.1))
}

/// How far the testbed's reader of one station's console has got (its
/// `Drain`): whether it is reading at all, whether it is inside a read and
/// the handing on of what it got, how many reads it has handed on, and how
/// many of those the last catch-up had taken in. Written on the reader's
/// thread, read on the loop's, here as in stations.printed.
#[pyclass(frozen, module = "ether_core")]
struct Marks {
    fd: i32,
    reading: AtomicBool,
    taking: AtomicBool,
    taken: AtomicU64,
    caught: AtomicU64,
}

#[pymethods]
impl Marks {
    #[new]
    fn new(fd: i32) -> Self {
        Marks {
            fd,
            reading: AtomicBool::new(false),
            taking: AtomicBool::new(false),
            taken: AtomicU64::new(0),
            caught: AtomicU64::new(0),
        }
    }

    #[getter]
    fn master(&self) -> i32 {
        self.fd
    }

    #[getter]
    fn reading(&self) -> bool {
        self.reading.load(Ordering::SeqCst)
    }

    #[setter]
    fn set_reading(&self, on: bool) {
        self.reading.store(on, Ordering::SeqCst)
    }

    #[getter]
    fn taking(&self) -> bool {
        self.taking.load(Ordering::SeqCst)
    }

    #[setter]
    fn set_taking(&self, on: bool) {
        self.taking.store(on, Ordering::SeqCst)
    }

    #[getter]
    fn taken(&self) -> u64 {
        self.taken.load(Ordering::SeqCst)
    }

    #[setter]
    fn set_taken(&self, n: u64) {
        self.taken.store(n, Ordering::SeqCst)
    }

    #[getter]
    fn caught(&self) -> u64 {
        self.caught.load(Ordering::SeqCst)
    }

    #[setter]
    fn set_caught(&self, n: u64) {
        self.caught.store(n, Ordering::SeqCst)
    }
}

/// The conductor's view of one station (ether.py's `Station`, its conductor
/// half; the medium's half stays in Python).
struct Station {
    addr: SocketAddr,
    seq: i64,
    idle: bool,
    until: Option<i64>,
    told: i64,
    asking: i64,
    asked_at: i64,
    standing: i64,
    granted_at: f64,
    idle_said: Option<(i64, Option<i64>)>,
    /// The number (`n`) of the last idle taken for the message it answers.
    idle_n: Option<i64>,
    unanswered: Vec<(i64, Vec<u8>)>,
    resent_at: f64,
    stale_said: Option<i64>,
    slow_idles: i64,
    /// Python holds callbacks for its next idle (`sync`).
    notify: bool,
    /// It takes several messages to a datagram, a line each (`lines` in its
    /// hello), and applies them as one.
    lines: bool,
    /// What the barrier has told it at this instant, sent as one datagram
    /// when the barrier stops (`kick_inner`).
    outbox: Vec<Vec<u8>>,
}

impl Station {
    fn new(addr: SocketAddr) -> Self {
        Station {
            addr,
            seq: 0,
            idle: false,
            until: None,
            told: 0,
            asking: 0,
            asked_at: 0,
            standing: 0,
            granted_at: 0.0,
            idle_said: None,
            idle_n: None,
            unanswered: Vec::new(),
            resent_at: 0.0,
            stale_said: None,
            slow_idles: 0,
            notify: false,
            lines: false,
            outbox: Vec::new(),
        }
    }
}

/// A `state` or `tx` held for the barrier: station, arrival, address, datagram.
type Held = (i64, u64, SocketAddr, Vec<u8>);

struct State {
    t: i64,
    stations: BTreeMap<i64, Station>,
    expected: BTreeSet<i64>,
    busy_count: i64,
    pending: Vec<Held>,
    /// Datagrams naming a station from an address other than the one it
    /// said hello from, dropped (`datagram`).
    strangers: i64,
    arrivals: u64,
    holds: i64,
    unread: i64,
    /// How many writes are waiting for a go-ahead (the list is Python's).
    asks: i64,
    dirty: BTreeSet<i64>,
    /// Whether Python reads what the stations printed before T moves.
    drain: bool,
    /// Whether that reading needs doing only when a console here shows
    /// something printed (`watches`), so Python is asked only then.
    watching: bool,
    /// Each station's console reader, as the testbed registered it.
    watches: BTreeMap<i64, Py<Marks>>,
    advancing: bool,
    /// Stations with something in their outbox, in the order they first had.
    outboxed: Vec<i64>,
    barriers: i64,
    runs: i64,
    resends: i64,
    standing: i64,
    /// The earliest event on Python's heap, None when it is empty.
    due: Option<i64>,
    closed: bool,
    // ---- instrumentation (Phase A, gated by `Core.counters_on`) ---------
    /// Datagrams off the wire, by what `datagram` found them to be.
    dg_idle: i64,
    dg_state: i64,
    dg_tx: i64,
    /// Not idle/state/tx: forwarded to Python as `_core_datagram`.
    dg_other: i64,
    /// `pump` calls, and the datagrams each one took off the inbox.
    pumps: i64,
    pump_dg_sum: i64,
    pump_dg_max: i64,
    pump_dg_eq0: i64,
    pump_dg_eq1: i64,
    /// Calls out to Python's `_core_*`, by which one.
    cb_flush: i64,
    cb_due: i64,
    cb_ask: i64,
    cb_paced: i64,
    cb_idle: i64,
    cb_drain: i64,
    cb_datagram: i64,
    cb_log: i64,
    // ---- Phase B instrumentation (also gated by `counters_on`) ----------
    /// `pump` entries from the event loop, vs. `pumps`' inner rounds: the
    /// quiescence loop's whole point is to make `pumps` >> `pump_calls`.
    pump_calls: i64,
    /// Rounds that returned for want of more within `pump_wait_us`.
    pump_timeouts: i64,
    /// Rounds that returned on `pump_cap_us`, work still waiting.
    pump_capped: i64,
    /// `send_many` calls, and the datagrams they carried (sendmmsg).
    send_batches: i64,
    send_batch_dg: i64,
}

impl State {
    /// A station's idle flag, and with it the count of stations that owe one.
    fn mark(&mut self, sid: i64, idle: bool) {
        if let Some(st) = self.stations.get_mut(&sid) {
            if st.idle != idle {
                st.idle = idle;
                self.busy_count += if idle { -1 } else { 1 };
            }
        }
    }

    /// Whether T must stay where it is: a station started and not heard from
    /// yet, one that owes an idle, a channel holding T, or a hold under way.
    fn busy(&self) -> bool {
        !self.expected.is_empty() || self.busy_count > 0 || self.unread > 0 || self.holds > 0
    }

    /// The next instant anything needs: the earliest event on Python's heap or
    /// a station's `until`, never before T; None when nothing waits.
    fn next_instant(&self) -> Option<i64> {
        let mut best = self.due;
        for st in self.stations.values() {
            if let Some(until) = st.until {
                best = Some(best.map_or(until, |b| b.min(until)));
            }
        }
        best.map(|b| b.max(self.t))
    }

    /// stations.printed over the watched consoles of `sids`: true when one
    /// has bytes waiting, or its reader is in a read or has handed on reads
    /// since it last caught up. The consoles are polled first, the marks
    /// looked at after, so bytes the reader took in between still count.
    fn printed(&self, sids: &[i64]) -> bool {
        let live: Vec<&Marks> = sids
            .iter()
            .filter_map(|sid| self.watches.get(sid))
            .map(|marks| marks.get())
            .filter(|marks| marks.reading.load(Ordering::SeqCst))
            .collect();
        if live.is_empty() {
            return false;
        }
        let mut fds: Vec<libc::pollfd> = live
            .iter()
            .map(|marks| libc::pollfd { fd: marks.fd, events: libc::POLLIN, revents: 0 })
            .collect();
        // SAFETY: a valid array of pollfds, polled without waiting.
        let found = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, 0) };
        live.iter().zip(fds.iter()).any(|(marks, fd)| {
            (found > 0 && fd.revents != 0)
                || marks.taking.load(Ordering::SeqCst)
                || marks.taken.load(Ordering::SeqCst) != marks.caught.load(Ordering::SeqCst)
        })
    }

    /// A message's number and T, up to the datagram: the station owes an idle
    /// for `seq`.
    fn grant(&mut self, sid: i64, t: Option<i64>) -> Option<(i64, i64)> {
        let now = self.t;
        let st = self.stations.get_mut(&sid)?;
        st.seq += 1;
        st.granted_at = monotonic();
        let seq = st.seq;
        let t = t.unwrap_or(now);
        st.told = st.told.max(t);
        self.mark(sid, false);
        Some((seq, t))
    }
}

/// What a step of the barrier found to do next (`advance`'s branches).
enum Step {
    Stop,
    Flush(Vec<Held>),
    Drain(Vec<i64>),
    To(i64),
}

#[pyclass(unsendable, module = "ether_core")]
struct Core {
    s: RefCell<State>,
    /// The CoreEther: the calls out to Python go to its `_core_*` methods.
    owner: RefCell<Option<PyObject>>,
    /// The ether's socket, which Python owns and closes.
    sock: ManuallyDrop<UdpSocket>,
    inbox: Arc<Inbox>,
    reader: RefCell<Option<JoinHandle<()>>>,
    paced: bool,
    standing_limit: i64,
    standing_quantum_us: i64,
    slow_idle_s: f64,
    resend_gap_s: f64,
    /// `ETHER_CORE_COUNTERS` read once at construction (Phase A instrumentation).
    counters_on: bool,
    /// `ETHER_CORE_PUMP_WAIT_US` (W): how long `pump` waits, GIL released,
    /// for the next datagram before giving the loop a turn. Default 200.
    pump_wait_us: u64,
    /// `ETHER_CORE_PUMP_CAP_US` (C): the most `pump` spends in one call from
    /// the loop before returning regardless, so other fds stay serviced.
    /// Default 5000.
    pump_cap_us: u64,
}

impl Core {
    fn call(&self, py: Python<'_>, name: &str, args: impl IntoPy<Py<PyTuple>>) -> PyResult<PyObject> {
        let owner = self.owner.borrow().as_ref().map(|o| o.clone_ref(py));
        match owner {
            Some(owner) => owner.call_method1(py, name, args),
            None => Ok(py.None()),
        }
    }

    fn log(&self, py: Python<'_>, msg: String) -> PyResult<()> {
        self.call(py, "_core_log", (msg,)).map(|_| ())
    }

    /// One line for simd.log at `close`, with Phase A's counters (gated by
    /// `counters_on`): datagrams by type, `pump` batches, wake pokes, and the
    /// `_core_*` callbacks, alongside the barrier/run/resend counts this
    /// state already kept.
    fn counters_report(&self) -> String {
        let s = self.s.borrow();
        let mean = if s.pumps > 0 { s.pump_dg_sum as f64 / s.pumps as f64 } else { 0.0 };
        format!(
            "core counters: datagrams idle={} state={} tx={} other={} total={} \
             | pumps={} dg_per_pump_mean={:.3} max={} eq0={} eq1={} wake_pokes={} drops={} \
             | runs={} barriers={} resends={} \
             | callbacks flush={} due={} ask={} paced={} idle={} drain={} datagram={} log={} \
             | pump_calls={} rounds_per_call={:.3} timeouts={} capped={} \
             | send_batches={} send_batch_dg={} dg_per_send_mean={:.3}",
            s.dg_idle,
            s.dg_state,
            s.dg_tx,
            s.dg_other,
            s.dg_idle + s.dg_state + s.dg_tx + s.dg_other,
            s.pumps,
            mean,
            s.pump_dg_max,
            s.pump_dg_eq0,
            s.pump_dg_eq1,
            self.inbox.wake_pokes.load(Ordering::SeqCst),
            self.inbox.drops.load(Ordering::SeqCst),
            s.runs,
            s.barriers,
            s.resends,
            s.cb_flush,
            s.cb_due,
            s.cb_ask,
            s.cb_paced,
            s.cb_idle,
            s.cb_drain,
            s.cb_datagram,
            s.cb_log,
            s.pump_calls,
            if s.pump_calls > 0 { s.pumps as f64 / s.pump_calls as f64 } else { 0.0 },
            s.pump_timeouts,
            s.pump_capped,
            s.send_batches,
            s.send_batch_dg,
            if s.send_batches > 0 { s.send_batch_dg as f64 / s.send_batches as f64 } else { 0.0 },
        )
    }

    /// One datagram out, as the event loop's transport sends it: a full
    /// send buffer is waited out, anything else is the datagram lost.
    fn send_raw(&self, data: &[u8], addr: SocketAddr) {
        if self.s.borrow().closed {
            return;
        }
        let mut retries = 0;
        loop {
            match self.sock.send_to(data, addr) {
                Ok(_) => return,
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == ErrorKind::WouldBlock && retries < SEND_RETRIES => {
                    retries += 1;
                    let mut pfd = libc::pollfd {
                        fd: self.sock.as_raw_fd(),
                        events: libc::POLLOUT,
                        revents: 0,
                    };
                    // SAFETY: one valid pollfd.
                    unsafe { libc::poll(&mut pfd, 1, SEND_RETRY_MS) };
                }
                Err(_) => return,
            }
        }
    }

    /// A `run` to a station due at T, the message the barrier itself sends:
    /// built and recorded here, sent with the rest of its step as one
    /// `send_many` batch (`step_to`).
    fn prepare_run(&self, sid: i64) -> Option<(SocketAddr, Vec<u8>)> {
        let mut s = self.s.borrow_mut();
        let (seq, t) = s.grant(sid, None)?;
        let data = format!("{{\"type\": \"run\", \"seq\": {}, \"t\": {}}}", seq, t).into_bytes();
        s.runs += 1;
        let st = s.stations.get_mut(&sid).expect("granted");
        st.unanswered.push((seq, data.clone()));
        Some((st.addr, data))
    }

    /// Several datagrams out in one syscall (`sendmmsg`), each to its own
    /// address, in the given order: a send the kernel cannot take at once is
    /// waited out exactly as `send_raw` does for one, resuming with whatever
    /// of the batch is left.
    fn send_many(&self, msgs: &[(SocketAddr, Vec<u8>)]) {
        if msgs.is_empty() || self.s.borrow().closed {
            return;
        }
        if self.counters_on {
            let mut s = self.s.borrow_mut();
            s.send_batches += 1;
            s.send_batch_dg += msgs.len() as i64;
        }
        let mut slots: Vec<(libc::sockaddr_storage, libc::socklen_t)> =
            msgs.iter().map(|(addr, _)| sockaddr_from(*addr)).collect();
        let mut iovecs: Vec<libc::iovec> = msgs
            .iter()
            // SAFETY: sendmmsg only reads through iov_base; iovec's type is
            // shared with recv, where it is written through.
            .map(|(_, data)| libc::iovec { iov_base: data.as_ptr() as *mut _, iov_len: data.len() })
            .collect();
        let mut hdrs: Vec<libc::mmsghdr> = (0..msgs.len())
            .map(|i| libc::mmsghdr {
                msg_hdr: libc::msghdr {
                    msg_name: (&mut slots[i].0 as *mut libc::sockaddr_storage).cast(),
                    msg_namelen: slots[i].1,
                    msg_iov: &mut iovecs[i],
                    msg_iovlen: 1,
                    msg_control: std::ptr::null_mut(),
                    msg_controllen: 0,
                    msg_flags: 0,
                },
                msg_len: 0,
            })
            .collect();
        let mut sent = 0usize;
        let mut retries = 0;
        while sent < hdrs.len() {
            // SAFETY: `hdrs[sent..]` and the iovec/sockaddr slots they point
            // into all live for this whole call.
            let rc = unsafe {
                libc::sendmmsg(self.sock.as_raw_fd(), hdrs[sent..].as_mut_ptr(), (hdrs.len() - sent) as _, 0)
            };
            if rc > 0 {
                sent += rc as usize;
                retries = 0;
                continue;
            }
            match std::io::Error::last_os_error().kind() {
                ErrorKind::Interrupted => continue,
                ErrorKind::WouldBlock if retries < SEND_RETRIES => {
                    retries += 1;
                    let mut pfd =
                        libc::pollfd { fd: self.sock.as_raw_fd(), events: libc::POLLOUT, revents: 0 };
                    // SAFETY: one valid pollfd.
                    unsafe { libc::poll(&mut pfd, 1, SEND_RETRY_MS) };
                }
                _ => break,
            }
        }
    }

    /// A datagram from a station: an idle, a state and a tx are the conductor's;
    /// anything else goes to Python as it came.
    fn datagram(&self, py: Python<'_>, data: &[u8], addr: SocketAddr) -> PyResult<()> {
        let parsed: Option<Value> = serde_json::from_slice(data).ok();
        if let Some(obj) = parsed.as_ref().and_then(Value::as_object) {
            let sid = obj.get("sid").and_then(as_int);
            let kind = obj.get("type").and_then(Value::as_str);
            // A station's radio speaks from the address it said hello from (the
            // time shim's wrote and listen have a socket of their own); a
            // restarted one says hello again, from its new one. Anything else
            // naming it is another's: on a shared host the stations of a run
            // whose ether is gone go on writing to its port, and once a new
            // run's ether has it their idles pass for its stations' (an idle a
            // station never said, a resend at the wall clock's moment).
            if let Some(sid) =
                sid.filter(|_| matches!(kind, Some("idle" | "state" | "tx" | "floor")))
            {
                let mut s = self.s.borrow_mut();
                if s.stations.get(&sid).is_some_and(|st| st.addr != addr) {
                    s.strangers += 1;
                    let first = s.strangers == 1;
                    drop(s);
                    if first {
                        self.log(
                            py,
                            format!(
                                "ether: a datagram for station {} from {}, not the address it said \
                                 hello from: dropped, as is any other (another run's station?)",
                                sid, addr
                            ),
                        )?;
                    }
                    return Ok(());
                }
            }
            match (kind, sid) {
                (Some("idle"), Some(sid)) => {
                    let seq = obj.get("seq").and_then(as_int);
                    let until = obj.get("until").and_then(as_truncated);
                    let n = obj.get("n").and_then(as_int);
                    return self.recv_idle_inner(py, sid, seq, until, n);
                }
                (Some("state") | Some("tx"), Some(sid)) => {
                    // Held for the barrier, and taken in station order there;
                    // anything a station says means it is not idle.
                    let mut s = self.s.borrow_mut();
                    if let Some(st) = s.stations.get_mut(&sid) {
                        st.addr = addr;
                        s.mark(sid, false);
                        s.arrivals += 1;
                        let arrival = s.arrivals;
                        s.pending.push((sid, arrival, addr, data.to_vec()));
                    }
                    return Ok(());
                }
                _ => {}
            }
        }
        self.call(py, "_core_datagram", (PyBytes::new_bound(py, data), addr_tuple(py, addr)))
            .map(|_| ())
    }

    /// A station's idle for message `seq`, wanting T again at `until`; it may
    /// let T move. An idle for an older message is the ordinary race once,
    /// and a lost message if it comes again: that one is sent again. A copy of
    /// the idle already taken for the current message (the same number `n`:
    /// the station says its last idle again on the wall clock until something
    /// comes back) is nothing, even once the station has said something since:
    /// taken then, it would pass for an idle at a moment the host chose.
    fn recv_idle_inner(&self, py: Python<'_>, sid: i64, seq: Option<i64>, until: Option<i64>,
                       n: Option<i64>) -> PyResult<()> {
        let Some(seq) = seq else { return Ok(()) };
        let mut resend = None;
        let mut said = None;
        let mut notify = false;
        {
            let mut s = self.s.borrow_mut();
            let t = s.t;
            if n.is_some() && s.stations.get(&sid).is_some_and(|st| st.seq == seq && st.idle_n == n) {
                return Ok(());
            }
            let Some(st) = s.stations.get_mut(&sid) else { return Ok(()) };
            if seq < st.seq {
                // Once is the ordinary race, an idle crossing the next message
                // on the wire; the same number again means that message never
                // came.
                if st.stale_said == Some(seq) {
                    resend = Some(seq);
                }
                st.stale_said = Some(seq);
            } else {
                st.stale_said = None;
                if seq != st.seq {
                    return Ok(());
                }
                st.idle_n = n;
                st.unanswered.clear();
                if st.idle && st.idle_said == Some((seq, until)) {
                    return Ok(());
                }
                st.idle_said = Some((seq, until));
                if monotonic() - st.granted_at >= self.slow_idle_s {
                    st.slow_idles += 1;
                }
                st.until = until;
                match until {
                    Some(u) if u <= t => {
                        // A station that keeps asking for the instant it already
                        // has is given the next tick's worth instead.
                        st.standing += 1;
                        if st.standing > self.standing_limit {
                            if st.standing == self.standing_limit + 1 {
                                said = Some(format!(
                                    "station {} asks for T {} again and again; giving it {} us",
                                    sid, t, self.standing_quantum_us
                                ));
                            }
                            st.until = Some(t + self.standing_quantum_us);
                        }
                    }
                    _ => st.standing = 0,
                }
                if st.notify {
                    st.notify = false;
                    notify = true;
                }
                s.mark(sid, true);
                s.dirty.insert(sid);
            }
        }
        if let Some(answered) = resend {
            return self.resend(py, sid, answered);
        }
        if let Some(msg) = said {
            self.log(py, msg)?;
        }
        if notify {
            self.call(py, "_core_idle", (sid,))?;
        }
        self.kick_inner(py)
    }

    /// What station `sid` was sent after message `answered`, sent again; at
    /// most once per resend gap.
    fn resend(&self, py: Python<'_>, sid: i64, answered: i64) -> PyResult<()> {
        let (missed, addr, said) = {
            let mut s = self.s.borrow_mut();
            let now = monotonic();
            let Some(st) = s.stations.get_mut(&sid) else { return Ok(()) };
            if now - st.resent_at < self.resend_gap_s {
                return Ok(());
            }
            st.resent_at = now;
            let missed: Vec<Vec<u8>> = st
                .unanswered
                .iter()
                .filter(|(seq, _)| *seq > answered)
                .map(|(_, data)| data.clone())
                .collect();
            let addr = st.addr;
            let mut said = None;
            if !missed.is_empty() {
                s.resends += missed.len() as i64;
                said = Some(format!(
                    "station {} missed {} message(s) after {}; sending them again",
                    sid,
                    missed.len(),
                    answered
                ));
            }
            (missed, addr, said)
        };
        if let Some(msg) = said {
            self.log(py, msg)?;
        }
        for data in missed {
            self.send_raw(&data, addr);
        }
        Ok(())
    }

    /// The barrier moved as far as it goes now (`advance`), never from inside
    /// itself, and then the outboxes sent.
    fn kick_inner(&self, py: Python<'_>) -> PyResult<()> {
        {
            let mut s = self.s.borrow_mut();
            if s.advancing || s.closed {
                return Ok(());
            }
            s.advancing = true;
        }
        let result = self.advance(py);
        self.s.borrow_mut().advancing = false;
        self.send_outboxes();
        result
    }

    /// Each station's messages of this instant in one datagram, a line each:
    /// it applies them together, so none of its threads sees part of T.
    fn send_outboxes(&self) {
        let out: Vec<(SocketAddr, Vec<u8>)> = {
            let mut s = self.s.borrow_mut();
            let sids = std::mem::take(&mut s.outboxed);
            sids.into_iter()
                .filter_map(|sid| {
                    let st = s.stations.get_mut(&sid)?;
                    let lines = std::mem::take(&mut st.outbox);
                    (!lines.is_empty()).then(|| (st.addr, lines.join(&b'\n')))
                })
                .collect()
        };
        self.send_many(&out);
    }

    fn advance(&self, py: Python<'_>) -> PyResult<()> {
        loop {
            if self.s.borrow().asks > 0 {
                // A write waiting for its go-ahead: Python decides whether the
                // run is quiet enough, and lets the first one go.
                if self.call(py, "_core_ask", ())?.is_truthy(py)? {
                    continue;
                }
            }
            let step = {
                let mut s = self.s.borrow_mut();
                if s.busy() {
                    Step::Stop
                } else if !s.pending.is_empty() {
                    let mut batch = std::mem::take(&mut s.pending);
                    batch.sort_by_key(|held| (held.0, held.1));
                    Step::Flush(batch)
                } else if !s.dirty.is_empty() && s.drain {
                    // What the stations printed at this T is read before T
                    // moves: a reply the testbed acts on is acted on here.
                    let sids: Vec<i64> = std::mem::take(&mut s.dirty).into_iter().collect();
                    if s.watching && !s.printed(&sids) {
                        // None of them printed: what the testbed would have
                        // answered (False), without asking it.
                        continue;
                    }
                    s.holds += 1;
                    Step::Drain(sids)
                } else {
                    match s.next_instant() {
                        None => Step::Stop,
                        Some(t) => Step::To(t),
                    }
                }
            };
            match step {
                Step::Stop => return Ok(()),
                Step::Flush(batch) => {
                    let batch: Vec<(i64, PyObject, PyObject)> = batch
                        .into_iter()
                        .map(|(sid, _, addr, data)| {
                            (sid, addr_tuple(py, addr), PyBytes::new_bound(py, &data).into_py(py))
                        })
                        .collect();
                    self.call(py, "_core_flush", (batch,))?;
                }
                Step::Drain(sids) => {
                    // Nothing printed: nothing to read and nothing set going,
                    // so T need not wait a turn of the loop for it.
                    if self.call(py, "_core_drain", (sids,))?.is_truthy(py)? {
                        self.s.borrow_mut().holds -= 1;
                    }
                }
                Step::To(t) => {
                    if self.paced && !self.call(py, "_core_paced", (t,))?.is_truthy(py)? {
                        return Ok(());
                    }
                    self.step_to(py, t)?;
                }
            }
        }
    }

    /// T moved to `t`: the events due on Python's heap are run, and each idle
    /// station due by `t` is sent its `run`. A T that stands is logged.
    fn step_to(&self, py: Python<'_>, t: i64) -> PyResult<()> {
        let (said, due) = {
            let mut s = self.s.borrow_mut();
            let mut said = None;
            if t == s.t {
                s.standing += 1;
                if s.standing % 10000 == 0 {
                    let due: Vec<String> = s
                        .stations
                        .iter()
                        .filter(|(_, st)| st.until.is_some_and(|u| u <= t))
                        .map(|(sid, _)| sid.to_string())
                        .collect();
                    said = Some(format!(
                        "T has stood at {} for {} steps; due: {}",
                        t,
                        s.standing,
                        due.join(", ")
                    ));
                }
            } else {
                s.standing = 0;
            }
            s.t = t;
            s.barriers += 1;
            (said, s.due.is_some_and(|d| d <= t))
        };
        if let Some(msg) = said {
            self.log(py, msg)?;
        }
        if due {
            self.call(py, "_core_due", ())?;
        }
        let sids: Vec<i64> = self
            .s
            .borrow()
            .stations
            .iter()
            .filter(|(_, st)| st.idle && st.until.is_some_and(|u| u <= t))
            .map(|(sid, _)| *sid)
            .collect();
        let msgs: Vec<(SocketAddr, Vec<u8>)> =
            sids.into_iter().filter_map(|sid| self.prepare_run(sid)).collect();
        self.send_many(&msgs);
        Ok(())
    }

    fn stop_reader(&self) {
        if let Some(reader) = self.reader.borrow_mut().take() {
            self.inbox.stop.store(true, Ordering::SeqCst);
            poke(self.inbox.halt.1);
            let _ = reader.join();
        }
    }

    fn with_station<R>(&self, sid: i64, f: impl FnOnce(&mut Station) -> R) -> PyResult<R> {
        let mut s = self.s.borrow_mut();
        match s.stations.get_mut(&sid) {
            Some(st) => Ok(f(st)),
            None => Err(PyKeyError::new_err(sid)),
        }
    }
}

#[pymethods]
impl Core {
    /// `fd` is the ether's bound, non-blocking UDP socket, which stays the
    /// caller's; `owner` takes the calls out (`_core_*`).
    #[new]
    #[pyo3(signature = (fd, owner, paced, standing_limit, standing_quantum_us, slow_idle_s, resend_gap_s))]
    fn new(
        fd: i32,
        owner: PyObject,
        paced: bool,
        standing_limit: i64,
        standing_quantum_us: i64,
        slow_idle_s: f64,
        resend_gap_s: f64,
    ) -> PyResult<Self> {
        let inbox = Arc::new(Inbox {
            queue: Mutex::new(VecDeque::new()),
            drops: AtomicU64::new(0),
            signaled: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            wake: pipe()?,
            halt: pipe()?,
            wake_pokes: AtomicU64::new(0),
            cv: Condvar::new(),
        });
        let reader = {
            let inbox = Arc::clone(&inbox);
            std::thread::Builder::new()
                .name("ether-reader".into())
                .spawn(move || inbox.read(fd))?
        };
        Ok(Core {
            s: RefCell::new(State {
                t: 0,
                stations: BTreeMap::new(),
                expected: BTreeSet::new(),
                busy_count: 0,
                pending: Vec::new(),
                strangers: 0,
                arrivals: 0,
                holds: 0,
                unread: 0,
                asks: 0,
                dirty: BTreeSet::new(),
                drain: false,
                watching: false,
                watches: BTreeMap::new(),
                advancing: false,
                outboxed: Vec::new(),
                barriers: 0,
                runs: 0,
                resends: 0,
                standing: 0,
                due: None,
                closed: false,
                dg_idle: 0,
                dg_state: 0,
                dg_tx: 0,
                dg_other: 0,
                pumps: 0,
                pump_dg_sum: 0,
                pump_dg_max: 0,
                pump_dg_eq0: 0,
                pump_dg_eq1: 0,
                cb_flush: 0,
                cb_due: 0,
                cb_ask: 0,
                cb_paced: 0,
                cb_idle: 0,
                cb_drain: 0,
                cb_datagram: 0,
                cb_log: 0,
                pump_calls: 0,
                pump_timeouts: 0,
                pump_capped: 0,
                send_batches: 0,
                send_batch_dg: 0,
            }),
            owner: RefCell::new(Some(owner)),
            // SAFETY: a socket fd the caller keeps open until close().
            sock: ManuallyDrop::new(unsafe { UdpSocket::from_raw_fd(fd) }),
            inbox,
            reader: RefCell::new(Some(reader)),
            paced,
            standing_limit,
            standing_quantum_us,
            slow_idle_s,
            resend_gap_s,
            counters_on: std::env::var_os("ETHER_CORE_COUNTERS").is_some_and(|v| v != "0"),
            pump_wait_us: std::env::var("ETHER_CORE_PUMP_WAIT_US")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(200),
            pump_cap_us: std::env::var("ETHER_CORE_PUMP_CAP_US")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(5000),
        })
    }

    /// What the event loop waits on: readable when datagrams are queued.
    #[getter]
    fn wake_fd(&self) -> i32 {
        self.inbox.wake.0
    }

    /// Datagrams the kernel has dropped on the ether's socket, as far as the
    /// reader has heard (SO_RXQ_OVFL, which comes with the next datagram).
    #[getter]
    fn drops(&self) -> u64 {
        self.inbox.drops.load(Ordering::SeqCst)
    }

    /// The datagrams queued, handled in the order they came; the event loop
    /// calls this when `wake_fd` is readable. What is not handled here, past
    /// the batch or behind a call out that raised, stays queued ahead of
    /// anything newer, and the loop is woken for it again.
    /// Phase B: pumps to quiescence. One call from the event loop now runs
    /// `datagram`/`advance` (same FIFO order, same callbacks inline with the
    /// GIL, as always) round after round, only waiting between rounds — with
    /// the GIL released, on a `Condvar` paired with the inbox's own mutex,
    /// not the wake pipe — for up to `pump_wait_us`. It returns to the loop
    /// (so the websocket, drivers and timers get a turn) when a wait finds
    /// nothing, or `pump_cap_us` total is up; the wake pipe is left armed
    /// for whichever of those is why, so the loop is woken again for it.
    fn pump(&self, py: Python<'_>) -> PyResult<()> {
        if self.counters_on {
            self.s.borrow_mut().pump_calls += 1;
        }
        let deadline = Instant::now() + Duration::from_micros(self.pump_cap_us);
        // The pipe emptied first, then the flag: a datagram queued in
        // between leaves the flag set and so no byte of its own, but it is
        // queued before the batch below is taken. The other way round a byte
        // could be emptied from under a flag still set, and what came after
        // it would wait with nothing to wake the loop. Done once here, not
        // every round below: resetting it on each round would have the
        // reader's every push re-poke the pipe on the edge it keeps seeing,
        // one real syscall per round, exactly the cost this loop exists to
        // avoid. Quiescence rounds take their wake from the Condvar instead;
        // the pipe is only looked at again when this call is about to return
        // (`rearm_if_pending`), which is also the only time it matters.
        empty(self.inbox.wake.0);
        self.inbox.signaled.store(false, Ordering::SeqCst);
        loop {
            let mut batch = {
                let mut queue = self.inbox.queue.lock().unwrap_or_else(|p| p.into_inner());
                let n = queue.len().min(PUMP_BATCH);
                queue.drain(..n).collect::<VecDeque<_>>()
            };
            if self.counters_on {
                let n = batch.len() as i64;
                let mut s = self.s.borrow_mut();
                s.pumps += 1;
                s.pump_dg_sum += n;
                s.pump_dg_max = s.pump_dg_max.max(n);
                match n {
                    0 => s.pump_dg_eq0 += 1,
                    1 => s.pump_dg_eq1 += 1,
                    _ => {}
                }
            }
            let mut result = Ok(());
            while let Some((data, addr)) = batch.pop_front() {
                if self.s.borrow().closed {
                    return Ok(());
                }
                if let Err(err) = self.datagram(py, &data, addr) {
                    result = Err(err);
                    break;
                }
            }
            {
                let mut queue = self.inbox.queue.lock().unwrap_or_else(|p| p.into_inner());
                while let Some(item) = batch.pop_back() {
                    queue.push_front(item);
                }
            }
            if result.is_err() {
                self.rearm_if_pending();
                return result;
            }
            if Instant::now() >= deadline {
                if self.counters_on {
                    self.s.borrow_mut().pump_capped += 1;
                }
                self.rearm_if_pending();
                return Ok(());
            }
            // Quiescence: wait for the next datagram with the GIL released.
            // Only `Inbox` (Send+Sync on its own: a Mutex and atomics) goes
            // into the closure, never `self` (`Core` is unsendable).
            let wait_us = self.pump_wait_us;
            let inbox = Arc::clone(&self.inbox);
            let more = py.allow_threads(move || {
                let queue = inbox.queue.lock().unwrap_or_else(|p| p.into_inner());
                if !queue.is_empty() {
                    return true;
                }
                let (queue, _timeout) = inbox
                    .cv
                    .wait_timeout(queue, Duration::from_micros(wait_us))
                    .unwrap_or_else(|p| p.into_inner());
                !queue.is_empty()
            });
            if !more {
                if self.counters_on {
                    self.s.borrow_mut().pump_timeouts += 1;
                }
                return Ok(());
            }
            // Looped back with more already queued: round again in Rust,
            // with no wake pipe and no asyncio iteration in between.
        }
    }

    /// `signal` iff the inbox still has work, so a caller returning to the
    /// event loop (on the cap or an error) leaves the wake pipe armed for it.
    fn rearm_if_pending(&self) {
        let pending = !self.inbox.queue.lock().unwrap_or_else(|p| p.into_inner()).is_empty();
        if pending {
            self.inbox.signal();
        }
    }

    /// Move T as far as the barrier lets it, now.
    fn kick(&self, py: Python<'_>) -> PyResult<()> {
        self.kick_inner(py)
    }

    /// An idle, as if it had come on the socket.
    #[pyo3(signature = (sid, seq, until=None))]
    fn recv_idle(&self, py: Python<'_>, sid: i64, seq: Option<i64>, until: Option<i64>) -> PyResult<()> {
        self.recv_idle_inner(py, sid, seq, until, None)
    }

    /// No more sends and no more calls out, and the reader stopped: the socket
    /// is the caller's to close.
    fn close(&self) {
        self.s.borrow_mut().closed = true;
        self.owner.borrow_mut().take();
        self.stop_reader();
    }

    fn busy(&self) -> bool {
        self.s.borrow().busy()
    }

    fn next_instant(&self) -> Option<i64> {
        self.s.borrow().next_instant()
    }

    // ---- counts and gates ---------------------------------------------

    #[getter]
    fn t(&self) -> i64 {
        self.s.borrow().t
    }

    #[getter]
    fn holds(&self) -> i64 {
        self.s.borrow().holds
    }

    /// How many datagrams named a station from an address not its own.
    #[getter]
    fn strangers(&self) -> i64 {
        self.s.borrow().strangers
    }

    #[setter]
    fn set_holds(&self, n: i64) {
        self.s.borrow_mut().holds = n;
    }

    #[getter]
    fn unread(&self) -> i64 {
        self.s.borrow().unread
    }

    #[setter]
    fn set_unread(&self, n: i64) {
        self.s.borrow_mut().unread = n;
    }

    #[getter]
    fn busy_count(&self) -> i64 {
        self.s.borrow().busy_count
    }

    #[setter]
    fn set_asks(&self, n: i64) {
        self.s.borrow_mut().asks = n;
    }

    #[setter]
    fn set_drain(&self, on: bool) {
        self.s.borrow_mut().drain = on;
    }

    /// The drain handler answers exactly stations.printed over the watched
    /// consoles, so it need be asked only when one of them shows something.
    #[setter]
    fn set_watching(&self, on: bool) {
        self.s.borrow_mut().watching = on;
    }

    /// Station `sid`'s console reader, from this start of it on.
    fn watch(&self, sid: i64, marks: Py<Marks>) {
        self.s.borrow_mut().watches.insert(sid, marks);
    }

    /// Station `sid`'s console reader `marks` has been let go.
    fn unwatch(&self, sid: i64, marks: Py<Marks>) {
        let mut s = self.s.borrow_mut();
        if s.watches.get(&sid).is_some_and(|held| held.is(&marks)) {
            s.watches.remove(&sid);
        }
    }

    #[setter]
    fn set_due(&self, t: Option<i64>) {
        self.s.borrow_mut().due = t;
    }

    #[getter]
    fn barriers(&self) -> i64 {
        self.s.borrow().barriers
    }

    #[getter]
    fn runs(&self) -> i64 {
        self.s.borrow().runs
    }

    #[getter]
    fn resends(&self) -> i64 {
        self.s.borrow().resends
    }

    #[getter]
    fn standing(&self) -> i64 {
        self.s.borrow().standing
    }

    fn pending_count(&self) -> usize {
        self.s.borrow().pending.len()
    }

    fn dirty(&self) -> Vec<i64> {
        self.s.borrow().dirty.iter().copied().collect()
    }

    // ---- stations -----------------------------------------------------

    fn expect(&self, sid: i64) {
        self.s.borrow_mut().expected.insert(sid);
    }

    fn unexpect(&self, sid: i64) {
        self.s.borrow_mut().expected.remove(&sid);
    }

    fn expected(&self) -> Vec<i64> {
        self.s.borrow().expected.iter().copied().collect()
    }

    fn has(&self, sid: i64) -> bool {
        self.s.borrow().stations.contains_key(&sid)
    }

    /// A station first heard from: not idle until it says so.
    fn add(&self, sid: i64, addr: (String, u16)) -> PyResult<()> {
        let addr = parse_addr(addr)?;
        let mut s = self.s.borrow_mut();
        if s.stations.insert(sid, Station::new(addr)).is_none() {
            s.busy_count += 1;
        } else {
            return Err(PyValueError::new_err(format!("station {} is already here", sid)));
        }
        Ok(())
    }

    /// A station gone, the conductor's half: True if there was one.
    fn forget(&self, sid: i64) -> bool {
        let mut s = self.s.borrow_mut();
        match s.stations.remove(&sid) {
            None => false,
            Some(st) => {
                if !st.idle {
                    s.busy_count -= 1;
                }
                s.pending.retain(|held| held.0 != sid);
                true
            }
        }
    }

    fn mark(&self, sid: i64, idle: bool) {
        self.s.borrow_mut().mark(sid, idle);
    }

    /// Python holds callbacks for this station's next idle.
    fn notify(&self, sid: i64) -> PyResult<()> {
        self.with_station(sid, |st| st.notify = true)
    }

    fn addr(&self, py: Python<'_>, sid: i64) -> PyResult<PyObject> {
        let addr = self.with_station(sid, |st| st.addr)?;
        Ok(addr_tuple(py, addr))
    }

    fn set_addr(&self, sid: i64, addr: (String, u16)) -> PyResult<()> {
        let addr = parse_addr(addr)?;
        self.with_station(sid, |st| st.addr = addr)
    }

    fn seq(&self, sid: i64) -> PyResult<i64> {
        self.with_station(sid, |st| st.seq)
    }

    fn idle(&self, sid: i64) -> PyResult<bool> {
        self.with_station(sid, |st| st.idle)
    }

    fn until(&self, sid: i64) -> PyResult<Option<i64>> {
        self.with_station(sid, |st| st.until)
    }

    #[pyo3(signature = (sid, until))]
    fn set_until(&self, sid: i64, until: Option<i64>) -> PyResult<()> {
        self.with_station(sid, |st| st.until = until)
    }

    fn told(&self, sid: i64) -> PyResult<i64> {
        self.with_station(sid, |st| st.told)
    }

    fn asking(&self, sid: i64) -> PyResult<i64> {
        self.with_station(sid, |st| st.asking)
    }

    fn set_asking(&self, sid: i64, n: i64) -> PyResult<()> {
        self.with_station(sid, |st| st.asking = n)
    }

    fn asked_at(&self, sid: i64) -> PyResult<i64> {
        self.with_station(sid, |st| st.asked_at)
    }

    fn set_asked_at(&self, sid: i64, seq: i64) -> PyResult<()> {
        self.with_station(sid, |st| st.asked_at = seq)
    }

    fn slow_idles(&self, sid: i64) -> PyResult<i64> {
        self.with_station(sid, |st| st.slow_idles)
    }

    // ---- sending ------------------------------------------------------

    /// A message for station `sid`, up to the datagram: its next number and the T
    /// the message carries (`t`, else T), or None for a station not here.
    #[pyo3(signature = (sid, t=None))]
    fn grant(&self, sid: i64, t: Option<i64>) -> Option<(i64, i64)> {
        let mut s = self.s.borrow_mut();
        if s.closed {
            return None;
        }
        s.grant(sid, t)
    }

    /// The datagram `grant` numbered: kept for a resend, and sent.
    fn post(&self, sid: i64, seq: i64, data: &[u8], run: bool) -> PyResult<()> {
        let addr = {
            let mut s = self.s.borrow_mut();
            if run {
                s.runs += 1;
            }
            let advancing = s.advancing;
            let Some(st) = s.stations.get_mut(&sid) else {
                return Err(PyKeyError::new_err(sid));
            };
            st.unanswered.push((seq, data.to_vec()));
            if advancing && st.lines {
                // The barrier's: this instant's datagram, sent when it stops.
                let first = st.outbox.is_empty();
                st.outbox.push(data.to_vec());
                if first {
                    s.outboxed.push(sid);
                }
                return Ok(());
            }
            st.addr
        };
        self.send_raw(data, addr);
        Ok(())
    }

    fn lines(&self, sid: i64) -> PyResult<bool> {
        self.with_station(sid, |st| st.lines)
    }

    /// Whether the station takes several messages to a datagram (its hello).
    fn set_lines(&self, sid: i64, on: bool) -> PyResult<()> {
        let mut s = self.s.borrow_mut();
        let st = s.stations.get_mut(&sid).ok_or_else(|| PyKeyError::new_err(sid))?;
        st.lines = on;
        Ok(())
    }

    /// A datagram to an address, outside any station's numbering.
    fn sendto(&self, data: &[u8], addr: (String, u16)) -> PyResult<()> {
        let addr = parse_addr(addr)?;
        self.send_raw(data, addr);
        Ok(())
    }

    // ---- the cycle with the owner ---------------------------------------

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(owner) = self.owner.try_borrow() {
            if let Some(owner) = owner.as_ref() {
                visit.call(owner)?;
            }
        }
        if let Ok(s) = self.s.try_borrow() {
            for marks in s.watches.values() {
                visit.call(marks)?;
            }
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.owner.get_mut().take();
        self.s.get_mut().watches.clear();
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        self.stop_reader();
        for fd in [self.inbox.wake.0, self.inbox.wake.1, self.inbox.halt.0, self.inbox.halt.1] {
            // SAFETY: the pipes are the core's own, and the reader has stopped.
            unsafe { libc::close(fd) };
        }
    }
}

#[pymodule]
fn ether_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Core>()?;
    m.add_class::<Marks>()?;
    Ok(())
}
