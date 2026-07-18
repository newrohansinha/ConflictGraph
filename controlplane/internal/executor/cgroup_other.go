//go:build !linux

package executor

import (
	"context"
	"errors"
)

type CgroupTracer struct{}

func NewCgroupTracer(_, _ string) (*CgroupTracer, error) {
	return nil, errors.New("cgroup tracing requires Linux with cgroup v2")
}

func (*CgroupTracer) Register(context.Context, string, string, int) (func(context.Context) error, error) {
	return nil, errors.New("cgroup tracing requires Linux with cgroup v2")
}
