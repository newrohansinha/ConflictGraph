#![no_std]
#![no_main]

use aya_ebpf::{
    helpers::{bpf_get_current_cgroup_id, bpf_get_current_pid_tgid, bpf_ktime_get_ns, bpf_probe_read_user_str_bytes},
    macros::{map, tracepoint},
    maps::{HashMap, RingBuf},
    programs::TracePointContext,
};

const KIND_FILE: u8 = 1;
const KIND_TCP: u8 = 3;
const KIND_PROCESS: u8 = 7;
const OP_READ: u8 = 1;
const OP_WRITE: u8 = 2;
const OP_DELETE: u8 = 4;
const OP_RENAME: u8 = 6;
const OP_BIND: u8 = 7;
const OP_CONNECT: u8 = 9;
const OP_EXEC: u8 = 10;
const OP_SPAWN: u8 = 11;
const OP_EXIT: u8 = 12;
const FLAG_TRUNC: i32 = 0o1000;
const FLAG_CREATE: i32 = 0o100;
const FLAG_WRONLY: i32 = 1;
const FLAG_RDWR: i32 = 2;

#[repr(C)]
#[derive(Copy, Clone)]
pub struct KernelEvent {
    pub timestamp_ns: u64,
    pub cgroup_id: u64,
    pub pid: u32,
    pub tid: u32,
    pub parent_pid: u32,
    pub kind: u8,
    pub operation: u8,
    pub access_mode: u8,
    pub flags: u8,
    pub result: i64,
    pub identifier_len: u16,
    pub sockaddr_len: u16,
    pub identifier: [u8; 256],
    pub sockaddr: [u8; 128],
}

impl KernelEvent {
    #[inline(always)]
    fn new(kind: u8, operation: u8) -> Self {
        let pid_tgid = bpf_get_current_pid_tgid();
        Self {
            timestamp_ns: unsafe { bpf_ktime_get_ns() },
            cgroup_id: unsafe { bpf_get_current_cgroup_id() },
            pid: (pid_tgid >> 32) as u32,
            tid: pid_tgid as u32,
            parent_pid: 0,
            kind,
            operation,
            access_mode: 0,
            flags: 0,
            result: 0,
            identifier_len: 0,
            sockaddr_len: 0,
            identifier: [0; 256],
            sockaddr: [0; 128],
        }
    }
}

#[map]
static EVENTS: RingBuf = RingBuf::with_byte_size(16 * 1024 * 1024, 0);

#[map]
static ENTER_EVENTS: HashMap<u64, KernelEvent> = HashMap::with_max_entries(32768, 0);

#[map]
static DROPPED: HashMap<u32, u64> = HashMap::with_max_entries(1, 0);

#[inline(always)]
fn submit(event: &KernelEvent) {
    if let Some(mut slot) = EVENTS.reserve::<KernelEvent>(0) {
        slot.write(*event);
        slot.submit(0);
    } else {
        let key = 0u32;
        let current = unsafe { DROPPED.get(&key).copied().unwrap_or(0) };
        let _ = DROPPED.insert(&key, &(current + 1), 0);
    }
}

#[inline(always)]
fn argument(ctx: &TracePointContext, offset: usize) -> Result<u64, i64> {
    unsafe { ctx.read_at::<u64>(offset).map_err(|_| -1) }
}

#[inline(always)]
fn capture_string(event: &mut KernelEvent, pointer: u64) {
    if pointer == 0 { return; }
    if let Ok(bytes) = unsafe { bpf_probe_read_user_str_bytes(pointer as *const u8, &mut event.identifier) } {
        event.identifier_len = bytes.len().min(255) as u16;
    }
}

#[tracepoint(category = "syscalls", name = "sys_enter_openat")]
pub fn sys_enter_openat(ctx: TracePointContext) -> u32 {
    match try_openat(ctx) { Ok(()) => 0, Err(_) => 1 }
}

fn try_openat(ctx: TracePointContext) -> Result<(), i64> {
    let filename = argument(&ctx, 24)?;
    let flags = argument(&ctx, 32)? as i32;
    let operation = if flags & (FLAG_WRONLY | FLAG_RDWR | FLAG_TRUNC | FLAG_CREATE) != 0 { OP_WRITE } else { OP_READ };
    let mut event = KernelEvent::new(KIND_FILE, operation);
    event.flags = (flags & 0xff) as u8;
    event.access_mode = if operation == OP_WRITE { 2 } else { 1 };
    capture_string(&mut event, filename);
    let key = bpf_get_current_pid_tgid();
    ENTER_EVENTS.insert(&key, &event, 0).map_err(|_| -1)
}

#[tracepoint(category = "syscalls", name = "sys_exit_openat")]
pub fn sys_exit_openat(ctx: TracePointContext) -> u32 {
    let key = bpf_get_current_pid_tgid();
    if let Some(stored) = unsafe { ENTER_EVENTS.get(&key) } {
        let mut event = *stored;
        event.result = unsafe { ctx.read_at::<i64>(16).unwrap_or(-1) };
        if event.result >= 0 { submit(&event); }
        let _ = ENTER_EVENTS.remove(&key);
    }
    0
}

#[tracepoint(category = "syscalls", name = "sys_enter_unlinkat")]
pub fn sys_enter_unlinkat(ctx: TracePointContext) -> u32 {
    let mut event = KernelEvent::new(KIND_FILE, OP_DELETE);
    if let Ok(pointer) = argument(&ctx, 24) { capture_string(&mut event, pointer); submit(&event); 0 } else { 1 }
}

#[tracepoint(category = "syscalls", name = "sys_enter_renameat2")]
pub fn sys_enter_renameat2(ctx: TracePointContext) -> u32 {
    let mut event = KernelEvent::new(KIND_FILE, OP_RENAME);
    if let Ok(pointer) = argument(&ctx, 24) { capture_string(&mut event, pointer); submit(&event); 0 } else { 1 }
}

#[tracepoint(category = "syscalls", name = "sys_enter_bind")]
pub fn sys_enter_bind(ctx: TracePointContext) -> u32 { socket_event(ctx, OP_BIND) }

#[tracepoint(category = "syscalls", name = "sys_enter_connect")]
pub fn sys_enter_connect(ctx: TracePointContext) -> u32 { socket_event(ctx, OP_CONNECT) }

#[inline(always)]
fn socket_event(ctx: TracePointContext, operation: u8) -> u32 {
    let pointer = match argument(&ctx, 24) { Ok(value) => value, Err(_) => return 1 };
    let length = match argument(&ctx, 32) { Ok(value) => value.min(128) as usize, Err(_) => return 1 };
    let mut event = KernelEvent::new(KIND_TCP, operation);
    if pointer != 0 && length > 0 {
        if unsafe { aya_ebpf::helpers::bpf_probe_read_user_buf(pointer as *const u8, &mut event.sockaddr[..length]) }.is_ok() {
            event.sockaddr_len = length as u16;
        }
    }
    submit(&event);
    0
}

#[tracepoint(category = "sched", name = "sched_process_exec")]
pub fn sched_process_exec(ctx: TracePointContext) -> u32 {
    let mut event = KernelEvent::new(KIND_PROCESS, OP_EXEC);
    if let Ok(pointer) = argument(&ctx, 8) { capture_string(&mut event, pointer); }
    submit(&event); 0
}

#[tracepoint(category = "sched", name = "sched_process_fork")]
pub fn sched_process_fork(ctx: TracePointContext) -> u32 {
    let mut event = KernelEvent::new(KIND_PROCESS, OP_SPAWN);
    event.parent_pid = unsafe { ctx.read_at::<u32>(24).unwrap_or(0) };
    event.result = unsafe { ctx.read_at::<u32>(44).unwrap_or(0) } as i64;
    submit(&event); 0
}

#[tracepoint(category = "sched", name = "sched_process_exit")]
pub fn sched_process_exit(_ctx: TracePointContext) -> u32 {
    submit(&KernelEvent::new(KIND_PROCESS, OP_EXIT)); 0
}

#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    unsafe { core::hint::unreachable_unchecked() }
}
