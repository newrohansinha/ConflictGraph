# Security

The tracer is the only component that needs elevated kernel capabilities. The Helm DaemonSet requests `BPF`, `PERFMON`, and `SYS_RESOURCE`, drops all other capabilities, uses runtime-default seccomp, and does not mount a service-account token. Some kernels or cluster policies still require a separately reviewed privileged deployment.

Test node IDs are passed to subprocess APIs as argument arrays, not shell strings. The reusable GitHub Action passes user inputs through environment variables and quotes each value. HTTP APIs are read-only and do not accept commands. Artifact categories and identifiers reject traversal, path separators, and whitespace.

The eBPF program copies bounded syscall and socket metadata. It does not inspect file contents, packets, request bodies, environment variables, or credentials. Unredacted paths can still contain sensitive names; use a secret hash salt and apply CI-log retention controls to raw traces.

The tracer’s Unix sockets are trusted local interfaces. Do not expose them over a network or mount them into untrusted containers. Restrict the runtime directory so only the runner and tracer can register identities or submit logical-resource events.

Model inference verifies artifact schema, feature order, and weight checksum. A checksum detects corruption, not a malicious artifact from a trusted location; control write access to the artifact volume. Database credentials and production path salts belong in secrets, not the example configuration.
