#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use serde::{Deserialize, Serialize};
use std::net::{Ipv4Addr, Ipv6Addr};

pub const IDENTIFIER_CAPACITY: usize = 256;
pub const SOCKADDR_CAPACITY: usize = 128;

#[repr(C)]
#[derive(Clone, Copy)]
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
    pub identifier: [u8; IDENTIFIER_CAPACITY],
    pub sockaddr: [u8; SOCKADDR_CAPACITY],
}

#[cfg(target_os = "linux")]
unsafe impl aya::Pod for KernelEvent {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ResourceType {
    File,
    Directory,
    TcpEndpoint,
    UdpEndpoint,
    UnixSocket,
    FileLock,
    Process,
    RedisKey,
    DatabaseResource,
    OtherLogicalResource,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Operation {
    Read,
    Write,
    Create,
    Delete,
    Lock,
    Rename,
    Bind,
    Listen,
    Connect,
    Exec,
    Spawn,
    Exit,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AccessMode {
    Read,
    Write,
    Exclusive,
    Shared,
    Execute,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NormalizedEvent {
    pub execution_id: String,
    pub test_id: String,
    pub timestamp_ns: u64,
    pub pid: u32,
    pub tid: u32,
    pub cgroup_id: u64,
    pub resource_type: ResourceType,
    pub resource_identifier: String,
    pub operation: Operation,
    pub access_mode: AccessMode,
    pub source: String,
    pub metadata: serde_json::Value,
    pub sequence: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum ParseError {
    #[error("unknown kernel resource kind {0}")]
    UnknownKind(u8),
    #[error("unknown kernel operation {0}")]
    UnknownOperation(u8),
    #[error("identifier was not valid UTF-8")]
    InvalidIdentifier,
    #[error("unsupported socket address family {0}")]
    UnsupportedAddress(u16),
    #[error("socket address was truncated")]
    TruncatedAddress,
}

pub fn resource_type(kind: u8, socket: &[u8]) -> Result<ResourceType, ParseError> {
    match kind {
        1 => Ok(ResourceType::File),
        2 => Ok(ResourceType::Directory),
        3 => {
            if socket.len() >= 2
                && u16::from_ne_bytes([socket[0], socket[1]]) == libc::AF_UNIX as u16
            {
                Ok(ResourceType::UnixSocket)
            } else {
                Ok(ResourceType::TcpEndpoint)
            }
        }
        4 => Ok(ResourceType::UdpEndpoint),
        7 => Ok(ResourceType::Process),
        value => Err(ParseError::UnknownKind(value)),
    }
}

pub fn operation(value: u8) -> Result<Operation, ParseError> {
    match value {
        1 => Ok(Operation::Read),
        2 => Ok(Operation::Write),
        3 => Ok(Operation::Create),
        4 => Ok(Operation::Delete),
        5 => Ok(Operation::Lock),
        6 => Ok(Operation::Rename),
        7 => Ok(Operation::Bind),
        8 => Ok(Operation::Listen),
        9 => Ok(Operation::Connect),
        10 => Ok(Operation::Exec),
        11 => Ok(Operation::Spawn),
        12 => Ok(Operation::Exit),
        other => Err(ParseError::UnknownOperation(other)),
    }
}

pub fn socket_identifier(bytes: &[u8]) -> Result<String, ParseError> {
    if bytes.len() < 2 {
        return Err(ParseError::TruncatedAddress);
    }
    let family = u16::from_ne_bytes([bytes[0], bytes[1]]);
    match family as i32 {
        libc::AF_INET => {
            if bytes.len() < 8 {
                return Err(ParseError::TruncatedAddress);
            }
            let port = u16::from_be_bytes([bytes[2], bytes[3]]);
            let address = Ipv4Addr::new(bytes[4], bytes[5], bytes[6], bytes[7]);
            Ok(format!("TCP:{address}:{port}"))
        }
        libc::AF_INET6 => {
            if bytes.len() < 24 {
                return Err(ParseError::TruncatedAddress);
            }
            let port = u16::from_be_bytes([bytes[2], bytes[3]]);
            let mut octets = [0u8; 16];
            octets.copy_from_slice(&bytes[8..24]);
            Ok(format!("TCP:[{}]:{port}", Ipv6Addr::from(octets)))
        }
        libc::AF_UNIX => {
            let path = bytes.get(2..).ok_or(ParseError::TruncatedAddress)?;
            let end = path
                .iter()
                .position(|value| *value == 0)
                .unwrap_or(path.len());
            let value = String::from_utf8_lossy(&path[..end]);
            Ok(format!("UNIX:{value}"))
        }
        other => Err(ParseError::UnsupportedAddress(other as u16)),
    }
}

pub fn identifier(event: &KernelEvent) -> Result<String, ParseError> {
    if event.sockaddr_len > 0 {
        return socket_identifier(
            &event.sockaddr[..usize::from(event.sockaddr_len).min(SOCKADDR_CAPACITY)],
        );
    }
    let length = usize::from(event.identifier_len).min(IDENTIFIER_CAPACITY);
    std::str::from_utf8(&event.identifier[..length])
        .map(|value| value.trim_end_matches('\0').to_owned())
        .map_err(|_| ParseError::InvalidIdentifier)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_ipv4_socket_resource() {
        let bytes = [2, 0, 0x1f, 0x90, 127, 0, 0, 1];
        assert_eq!(socket_identifier(&bytes).unwrap(), "TCP:127.0.0.1:8080");
    }

    #[test]
    fn parses_unix_socket_resource() {
        let mut bytes = vec![libc::AF_UNIX as u8, 0];
        bytes.extend_from_slice(b"/tmp/service.sock\0");
        assert_eq!(socket_identifier(&bytes).unwrap(), "UNIX:/tmp/service.sock");
        assert_eq!(resource_type(3, &bytes).unwrap(), ResourceType::UnixSocket);
    }

    #[test]
    fn rejects_truncated_socket() {
        assert!(matches!(
            socket_identifier(&[2]),
            Err(ParseError::TruncatedAddress)
        ));
    }

    #[test]
    fn maps_all_supported_operations() {
        for value in 1..=12 {
            assert!(operation(value).is_ok(), "operation {value} was not mapped");
        }
        assert!(matches!(
            operation(99),
            Err(ParseError::UnknownOperation(99))
        ));
    }

    #[test]
    fn extracts_bounded_identifier() {
        let mut kernel: KernelEvent = unsafe { core::mem::zeroed() };
        kernel.identifier[..11].copy_from_slice(b"/tmp/a.db\0\0");
        kernel.identifier_len = 11;
        assert_eq!(identifier(&kernel).unwrap(), "/tmp/a.db");
        assert_eq!(IDENTIFIER_CAPACITY, 256);
        assert_eq!(SOCKADDR_CAPACITY, 128);
    }
}
