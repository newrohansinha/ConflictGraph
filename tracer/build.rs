fn main() {
    println!("cargo:rerun-if-env-changed=CONFLICTGRAPH_EBPF_OBJECT");
    println!("cargo:rerun-if-changed=ebpf/src/main.rs");
}
