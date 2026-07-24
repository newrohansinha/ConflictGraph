"use client";
export default function GlobalError({
  error,
  reset,
}: {
  error: Error;
  reset: () => void;
}) {
  return (
    <div className="empty error">
      <h2>Something went wrong</h2>
      <p>{error.message}</p>
      <button className="button" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
