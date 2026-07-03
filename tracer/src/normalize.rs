#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use crate::event::{AccessMode, NormalizedEvent, Operation, ResourceType};
use sha2::{Digest, Sha256};
use std::{
    path::{Component, Path, PathBuf},
    sync::Arc,
};

#[derive(Debug, Clone)]
pub struct Policy {
    pub excluded_prefixes: Arc<Vec<PathBuf>>,
    pub redact_paths: bool,
    pub hash_salt: Arc<String>,
    pub repository_root: Option<PathBuf>,
    pub include_system_reads: bool,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            excluded_prefixes: Arc::new(vec![
                "/proc".into(),
                "/sys".into(),
                "/usr/lib".into(),
                "/lib".into(),
            ]),
            redact_paths: false,
            hash_salt: Arc::new(String::new()),
            repository_root: None,
            include_system_reads: false,
        }
    }
}

impl Policy {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.redact_paths && self.hash_salt.is_empty() {
            Err("path redaction requires a non-empty salt")
        } else {
            Ok(())
        }
    }
}

pub struct Normalizer {
    policy: Policy,
}
impl Normalizer {
    pub fn new(policy: Policy) -> Result<Self, &'static str> {
        policy.validate()?;
        Ok(Self { policy })
    }
    pub fn normalize(&self, event: &mut NormalizedEvent) -> bool {
        match event.resource_type {
            ResourceType::File
            | ResourceType::Directory
            | ResourceType::UnixSocket
            | ResourceType::FileLock
            | ResourceType::DatabaseResource => {
                match self.path(&event.resource_identifier, &event.operation) {
                    Some(value) => event.resource_identifier = value,
                    None => return false,
                }
            }
            ResourceType::TcpEndpoint | ResourceType::UdpEndpoint => {
                event.resource_identifier = event.resource_identifier.to_ascii_uppercase()
            }
            ResourceType::RedisKey if !event.resource_identifier.starts_with("REDIS:") => {
                event.resource_identifier = format!("REDIS:{}", event.resource_identifier)
            }
            _ => {}
        }
        true
    }
    fn path(&self, raw: &str, operation: &Operation) -> Option<String> {
        if raw.is_empty() {
            return None;
        };
        let source = Path::new(raw);
        let mut normalized = PathBuf::new();
        for component in source.components() {
            match component {
                Component::ParentDir => {
                    normalized.pop();
                }
                Component::CurDir => {}
                other => normalized.push(other.as_os_str()),
            }
        }
        if matches!(operation, Operation::Read)
            && !self.policy.include_system_reads
            && self
                .policy
                .excluded_prefixes
                .iter()
                .any(|prefix| normalized.starts_with(prefix))
        {
            return None;
        }
        let mut value = if let Some(root) = &self.policy.repository_root {
            if let Ok(relative) = normalized.strip_prefix(root) {
                format!("$REPO/{}", relative.display())
            } else {
                normalized.to_string_lossy().into_owned()
            }
        } else {
            normalized.to_string_lossy().into_owned()
        };
        if self.policy.redact_paths {
            let mut hash = Sha256::new();
            hash.update(self.policy.hash_salt.as_bytes());
            hash.update([0]);
            hash.update(value.as_bytes());
            value = format!("sha256:{:x}", hash.finalize())
        };
        Some(value)
    }
}

pub fn access_mode(operation: &Operation, raw: u8) -> AccessMode {
    match raw {
        1 => AccessMode::Read,
        2 => AccessMode::Write,
        3 => AccessMode::Exclusive,
        4 => AccessMode::Shared,
        5 => AccessMode::Execute,
        _ => match operation {
            Operation::Read => AccessMode::Read,
            Operation::Exec => AccessMode::Execute,
            Operation::Bind | Operation::Lock => AccessMode::Exclusive,
            Operation::Write | Operation::Create | Operation::Delete | Operation::Rename => {
                AccessMode::Write
            }
            _ => AccessMode::Unknown,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filters_system_reads_but_retains_writes() {
        let normalizer = Normalizer::new(Policy::default()).unwrap();
        let mut read = fixture("/usr/lib/libc.so", Operation::Read);
        let mut write = fixture("/usr/lib/runtime-state", Operation::Write);
        assert!(!normalizer.normalize(&mut read));
        assert!(normalizer.normalize(&mut write));
    }

    #[test]
    fn repository_paths_receive_stable_alias() {
        let policy = Policy {
            repository_root: Some("/workspace/repo".into()),
            ..Policy::default()
        };
        let normalizer = Normalizer::new(policy).unwrap();
        let mut event = fixture("/workspace/repo/tests/test_a.py", Operation::Write);
        assert!(normalizer.normalize(&mut event));
        assert_eq!(event.resource_identifier, "$REPO/tests/test_a.py");
    }

    #[test]
    fn redaction_requires_salt_and_is_stable() {
        let invalid = Policy {
            redact_paths: true,
            ..Policy::default()
        };
        assert!(Normalizer::new(invalid).is_err());
        let valid = Policy {
            redact_paths: true,
            hash_salt: Arc::new("salt".into()),
            ..Policy::default()
        };
        let normalizer = Normalizer::new(valid).unwrap();
        let mut left = fixture("/tmp/private", Operation::Write);
        let mut right = fixture("/tmp/private", Operation::Write);
        normalizer.normalize(&mut left);
        normalizer.normalize(&mut right);
        assert_eq!(left.resource_identifier, right.resource_identifier);
        assert!(left.resource_identifier.starts_with("sha256:"));
        assert!(!left.resource_identifier.contains("private"));
    }

    #[test]
    fn derives_semantic_access_modes() {
        assert_eq!(access_mode(&Operation::Read, 0), AccessMode::Read);
        assert_eq!(access_mode(&Operation::Bind, 0), AccessMode::Exclusive);
        assert_eq!(access_mode(&Operation::Write, 0), AccessMode::Write);
        assert_eq!(access_mode(&Operation::Connect, 0), AccessMode::Unknown);
    }

    fn fixture(identifier: &str, operation: Operation) -> NormalizedEvent {
        NormalizedEvent {
            execution_id: "execution".into(),
            test_id: "test".into(),
            timestamp_ns: 1,
            pid: 1,
            tid: 1,
            cgroup_id: 1,
            resource_type: ResourceType::File,
            resource_identifier: identifier.into(),
            operation,
            access_mode: AccessMode::Unknown,
            source: "REPLAY".into(),
            metadata: serde_json::Value::Null,
            sequence: 1,
        }
    }
}
