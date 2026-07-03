#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Registration {
    pub execution_id: String,
    pub test_id: String,
    pub cgroup_id: u64,
    pub root_pid: u32,
    pub registered_at_ns: u128,
}

#[derive(Default)]
struct Inner {
    cgroups: HashMap<u64, Registration>,
    processes: HashMap<u32, Registration>,
    parents: HashMap<u32, u32>,
}

#[derive(Clone, Default)]
pub struct AttributionRegistry {
    inner: Arc<RwLock<Inner>>,
}

impl AttributionRegistry {
    pub fn register(
        &self,
        execution_id: String,
        test_id: String,
        cgroup_id: u64,
        root_pid: u32,
    ) -> Result<Registration, &'static str> {
        if execution_id.is_empty() || test_id.is_empty() {
            return Err("execution_id and test_id are required");
        }
        if cgroup_id == 0 && root_pid == 0 {
            return Err("either cgroup_id or root_pid is required");
        }
        let registration = Registration {
            execution_id,
            test_id,
            cgroup_id,
            root_pid,
            registered_at_ns: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos(),
        };
        let mut state = self.inner.write();
        if cgroup_id != 0 {
            state.cgroups.insert(cgroup_id, registration.clone());
        }
        if root_pid != 0 {
            state.processes.insert(root_pid, registration.clone());
        }
        Ok(registration)
    }

    pub fn fork(&self, parent: u32, child: u32) {
        let mut state = self.inner.write();
        state.parents.insert(child, parent);
        if let Some(registration) = state.processes.get(&parent).cloned() {
            state.processes.insert(child, registration);
        }
    }

    pub fn resolve(&self, cgroup_id: u64, pid: u32) -> Option<Registration> {
        let state = self.inner.read();
        if cgroup_id != 0 {
            if let Some(value) = state.cgroups.get(&cgroup_id) {
                return Some(value.clone());
            }
        }
        let mut current = pid;
        let mut seen = HashSet::with_capacity(8);
        for _ in 0..64 {
            if current == 0 || !seen.insert(current) {
                break;
            }
            if let Some(value) = state.processes.get(&current) {
                return Some(value.clone());
            }
            current = state.parents.get(&current).copied().unwrap_or(0);
        }
        None
    }

    pub fn exit(&self, pid: u32) {
        let mut state = self.inner.write();
        state.processes.remove(&pid);
        state.parents.remove(&pid);
    }

    pub fn unregister(&self, execution_id: &str) {
        let mut state = self.inner.write();
        state
            .cgroups
            .retain(|_, value| value.execution_id != execution_id);
        state
            .processes
            .retain(|_, value| value.execution_id != execution_id);
        let live: HashSet<u32> = state.processes.keys().copied().collect();
        state
            .parents
            .retain(|child, parent| live.contains(child) || live.contains(parent));
    }

    #[cfg(test)]
    pub fn counts(&self) -> (usize, usize) {
        let state = self.inner.read();
        (state.cgroups.len(), state.processes.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cgroup_resolution_precedes_process_fallback() {
        let registry = AttributionRegistry::default();
        registry
            .register("process".into(), "test-process".into(), 0, 100)
            .unwrap();
        registry
            .register("cgroup".into(), "test-cgroup".into(), 55, 0)
            .unwrap();
        assert_eq!(registry.resolve(55, 100).unwrap().execution_id, "cgroup");
    }

    #[test]
    fn descendants_inherit_registration() {
        let registry = AttributionRegistry::default();
        registry
            .register("execution".into(), "test".into(), 0, 100)
            .unwrap();
        registry.fork(100, 101);
        registry.fork(101, 102);
        assert_eq!(registry.resolve(0, 102).unwrap().test_id, "test");
        assert_eq!(registry.counts(), (0, 3));
    }

    #[test]
    fn exit_and_unregister_remove_entries() {
        let registry = AttributionRegistry::default();
        registry
            .register("execution".into(), "test".into(), 9, 100)
            .unwrap();
        registry.fork(100, 101);
        registry.exit(101);
        assert!(registry.resolve(0, 101).is_none());
        registry.unregister("execution");
        assert!(registry.resolve(9, 100).is_none());
        assert_eq!(registry.counts(), (0, 0));
    }

    #[test]
    fn incomplete_registration_is_rejected() {
        let registry = AttributionRegistry::default();
        assert!(registry
            .register(String::new(), "test".into(), 1, 0)
            .is_err());
        assert!(registry
            .register("execution".into(), String::new(), 1, 0)
            .is_err());
        assert!(registry
            .register("execution".into(), "test".into(), 0, 0)
            .is_err());
    }
}
