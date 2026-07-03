mod attribution;
mod event;
mod normalize;

use anyhow::{Context, Result};
use attribution::AttributionRegistry;
#[cfg(target_os = "linux")]
use aya::{
    maps::{HashMap as AyaHashMap, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use clap::{Parser, ValueEnum};
use event::NormalizedEvent;
#[cfg(target_os = "linux")]
use event::{identifier, operation, resource_type, KernelEvent};
use metrics::counter;
#[cfg(target_os = "linux")]
use metrics::gauge;
use metrics_exporter_prometheus::PrometheusBuilder;
#[cfg(target_os = "linux")]
use normalize::access_mode;
use normalize::{Normalizer, Policy};
#[cfg(target_os = "linux")]
use parking_lot::Mutex;
use serde::Deserialize;
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{atomic::AtomicU64, Arc},
};
#[cfg(target_os = "linux")]
use std::{mem, sync::atomic::Ordering};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{UnixDatagram, UnixListener},
    signal,
    sync::mpsc,
};
use tracing::{info, warn};

#[derive(Debug, Clone, ValueEnum)]
enum Mode {
    Ebpf,
    Replay,
}
#[derive(Debug, Parser)]
#[command(
    name = "conflictgraph-tracer",
    version,
    about = "Kernel resource tracer for ConflictGraph"
)]
struct Arguments {
    #[arg(long, value_enum, default_value = "ebpf")]
    mode: Mode,
    #[arg(long, default_value = "/run/conflictgraph/tracer.sock")]
    control_socket: PathBuf,
    #[arg(long, default_value = "/run/conflictgraph/logical.sock")]
    logical_socket: PathBuf,
    #[arg(long, default_value = "-")]
    output: String,
    #[arg(long)]
    replay: Option<PathBuf>,
    #[arg(long, default_value = "127.0.0.1:9465")]
    metrics_address: String,
    #[arg(long)]
    redact_paths: bool,
    #[arg(long, env = "CONFLICTGRAPH_HASH_SALT", default_value = "")]
    hash_salt: String,
    #[arg(long)]
    repository_root: Option<PathBuf>,
    #[arg(long, default_value_t = 65536)]
    queue_capacity: usize,
    #[arg(
        long,
        default_value = "tracer/target/bpfel-unknown-none/release/conflictgraph-ebpf"
    )]
    ebpf_object: PathBuf,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
enum ControlMessage {
    Register {
        execution_id: String,
        test_id: String,
        cgroup_id: u64,
        root_pid: u32,
    },
    Unregister {
        execution_id: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let arguments = Arguments::parse();
    let metrics_address: std::net::SocketAddr = arguments.metrics_address.parse()?;
    PrometheusBuilder::new()
        .with_http_listener(metrics_address)
        .install()
        .context("install metrics exporter")?;
    let registry = AttributionRegistry::default();
    let policy = Policy {
        redact_paths: arguments.redact_paths,
        hash_salt: Arc::new(arguments.hash_salt.clone()),
        repository_root: arguments.repository_root.clone(),
        ..Policy::default()
    };
    let normalizer = Arc::new(Normalizer::new(policy).map_err(anyhow::Error::msg)?);
    let (sender, receiver) = mpsc::channel(arguments.queue_capacity);
    let writer = tokio::spawn(write_events(arguments.output.clone(), receiver));
    let control = tokio::spawn(control_server(
        arguments.control_socket.clone(),
        registry.clone(),
    ));
    let logical = tokio::spawn(logical_server(
        arguments.logical_socket.clone(),
        sender.clone(),
        normalizer.clone(),
    ));
    let sequence = Arc::new(AtomicU64::new(0));
    let trace = match arguments.mode {
        Mode::Replay => tokio::spawn(replay(
            arguments
                .replay
                .context("--replay is required in replay mode")?,
            sender,
            normalizer,
        )),
        Mode::Ebpf => tokio::spawn(run_ebpf(
            arguments.ebpf_object,
            sender,
            registry,
            normalizer,
            sequence,
        )),
    };
    signal::ctrl_c().await?;
    info!("shutdown requested");
    trace.abort();
    control.abort();
    logical.abort();
    let _ = writer.await;
    Ok(())
}

async fn write_events(output: String, mut receiver: mpsc::Receiver<NormalizedEvent>) -> Result<()> {
    let mut writer: Box<dyn tokio::io::AsyncWrite + Unpin + Send> = if output == "-" {
        Box::new(tokio::io::stdout())
    } else {
        Box::new(tokio::fs::File::create(output).await?)
    };
    while let Some(event) = receiver.recv().await {
        let mut value = serde_json::to_vec(&event)?;
        value.push(b'\n');
        writer.write_all(&value).await?;
        counter!("conflictgraph_tracer_events_processed_total").increment(1)
    }
    writer.flush().await?;
    Ok(())
}

async fn control_server(path: PathBuf, registry: AttributionRegistry) -> Result<()> {
    prepare_socket(&path)?;
    let listener = UnixListener::bind(&path)?;
    loop {
        let (stream, _) = listener.accept().await?;
        let registry = registry.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stream).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match serde_json::from_str::<ControlMessage>(&line) {
                    Ok(ControlMessage::Register {
                        execution_id,
                        test_id,
                        cgroup_id,
                        root_pid,
                    }) => {
                        if let Err(reason) =
                            registry.register(execution_id, test_id, cgroup_id, root_pid)
                        {
                            warn!(reason, "rejected attribution registration")
                        }
                    }
                    Ok(ControlMessage::Unregister { execution_id }) => {
                        registry.unregister(&execution_id)
                    }
                    Err(error) => {
                        counter!("conflictgraph_tracer_parse_failures_total").increment(1);
                        warn!(%error,"invalid control message")
                    }
                }
            }
        });
    }
}

async fn logical_server(
    path: PathBuf,
    sender: mpsc::Sender<NormalizedEvent>,
    normalizer: Arc<Normalizer>,
) -> Result<()> {
    prepare_socket(&path)?;
    let socket = UnixDatagram::bind(&path)?;
    let mut buffer = vec![0u8; 8192];
    loop {
        let size = socket.recv(&mut buffer).await?;
        match serde_json::from_slice::<NormalizedEvent>(&buffer[..size]) {
            Ok(mut event) => {
                if normalizer.normalize(&mut event) && sender.try_send(event).is_err() {
                    counter!("conflictgraph_tracer_events_dropped_total", "source"=>"logical")
                        .increment(1)
                }
            }
            Err(error) => {
                warn!(%error,"invalid logical event");
                counter!("conflictgraph_tracer_parse_failures_total").increment(1)
            }
        }
    }
}

async fn replay(
    path: PathBuf,
    sender: mpsc::Sender<NormalizedEvent>,
    normalizer: Arc<Normalizer>,
) -> Result<()> {
    let file = tokio::fs::File::open(path).await?;
    let mut lines = BufReader::new(file).lines();
    while let Some(line) = lines.next_line().await? {
        let mut event: NormalizedEvent = serde_json::from_str(&line)?;
        event.source = "REPLAY".into();
        if normalizer.normalize(&mut event) {
            sender.send(event).await?
        }
    }
    Ok(())
}

#[cfg(target_os = "linux")]
async fn run_ebpf(
    object: PathBuf,
    sender: mpsc::Sender<NormalizedEvent>,
    registry: AttributionRegistry,
    normalizer: Arc<Normalizer>,
    sequence: Arc<AtomicU64>,
) -> Result<()> {
    let object_bytes = tokio::fs::read(&object)
        .await
        .with_context(|| format!("read eBPF object {}", object.display()))?;
    let mut ebpf = Ebpf::load(&object_bytes).context("load eBPF object")?;
    for (program, category, event) in [
        ("sys_enter_openat", "syscalls", "sys_enter_openat"),
        ("sys_exit_openat", "syscalls", "sys_exit_openat"),
        ("sys_enter_unlinkat", "syscalls", "sys_enter_unlinkat"),
        ("sys_enter_renameat2", "syscalls", "sys_enter_renameat2"),
        ("sys_enter_bind", "syscalls", "sys_enter_bind"),
        ("sys_enter_connect", "syscalls", "sys_enter_connect"),
        ("sched_process_exec", "sched", "sched_process_exec"),
        ("sched_process_fork", "sched", "sched_process_fork"),
        ("sched_process_exit", "sched", "sched_process_exit"),
    ] {
        let tracepoint: &mut TracePoint = ebpf
            .program_mut(program)
            .context("missing eBPF program")?
            .try_into()?;
        tracepoint.load()?;
        tracepoint.attach(category, event)?;
    }
    let ring = RingBuf::try_from(ebpf.take_map("EVENTS").context("EVENTS map missing")?)?;
    let dropped = AyaHashMap::<_, u32, u64>::try_from(
        ebpf.take_map("DROPPED").context("DROPPED map missing")?,
    )?;
    let ring = Arc::new(Mutex::new(ring));
    let mut last_dropped = 0;
    info!("eBPF programs loaded");
    loop {
        let mut handled = 0;
        {
            let mut locked = ring.lock();
            while let Some(item) = locked.next() {
                handled += 1;
                counter!("conflictgraph_tracer_events_captured_total").increment(1);
                if item.len() < mem::size_of::<KernelEvent>() {
                    counter!("conflictgraph_tracer_parse_failures_total").increment(1);
                    continue;
                };
                let kernel =
                    unsafe { std::ptr::read_unaligned(item.as_ptr() as *const KernelEvent) };
                if kernel.operation == 11 {
                    registry.fork(kernel.parent_pid, kernel.result.max(0) as u32)
                };
                let Some(registration) = registry.resolve(kernel.cgroup_id, kernel.pid) else {
                    counter!("conflictgraph_tracer_unattributed_events_total").increment(1);
                    continue;
                };
                if kernel.operation == 12 {
                    registry.exit(kernel.pid);
                }
                let resource_type = match resource_type(
                    kernel.kind,
                    &kernel.sockaddr[..usize::from(kernel.sockaddr_len).min(128)],
                ) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                let operation = match operation(kernel.operation) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                let resource_identifier = match identifier(&kernel) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                let mut event = NormalizedEvent {
                    execution_id: registration.execution_id,
                    test_id: registration.test_id,
                    timestamp_ns: kernel.timestamp_ns,
                    pid: kernel.pid,
                    tid: kernel.tid,
                    cgroup_id: kernel.cgroup_id,
                    resource_type,
                    resource_identifier,
                    access_mode: access_mode(&operation, kernel.access_mode),
                    operation,
                    source: "EBPF".into(),
                    metadata: serde_json::json!({"result":kernel.result,"flags":kernel.flags}),
                    sequence: sequence.fetch_add(1, Ordering::Relaxed),
                };
                if normalizer.normalize(&mut event) && sender.try_send(event).is_err() {
                    counter!("conflictgraph_tracer_events_dropped_total", "source"=>"ebpf")
                        .increment(1)
                }
            }
        }
        if let Ok(total) = dropped.get(&0, 0) {
            if total > last_dropped {
                counter!("conflictgraph_tracer_events_dropped_total", "source"=>"kernel")
                    .increment(total - last_dropped);
            }
            last_dropped = total;
        }
        gauge!("conflictgraph_tracer_ring_buffer_events_last_poll").set(handled as f64);
        tokio::time::sleep(std::time::Duration::from_millis(if handled == 0 {
            5
        } else {
            0
        }))
        .await
    }
}

#[cfg(not(target_os = "linux"))]
async fn run_ebpf(
    _object: PathBuf,
    _sender: mpsc::Sender<NormalizedEvent>,
    _registry: AttributionRegistry,
    _normalizer: Arc<Normalizer>,
    _sequence: Arc<AtomicU64>,
) -> Result<()> {
    anyhow::bail!("real eBPF tracing is Linux-only; use --mode replay on this host")
}

fn prepare_socket(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?
    };
    if path.exists() {
        fs::remove_file(path)?
    };
    Ok(())
}
