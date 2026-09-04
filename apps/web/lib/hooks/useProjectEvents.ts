"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ACTIVE_JOB_STATUSES, type Job, jobsApi } from "@/lib/api";

/** What the server pushes. Events carry invalidations, never entity bodies —
 *  the client refetches through the normal path, so a dropped message costs a
 *  round trip instead of leaving a stale copy it believes is fresh. */
type ServerEvent =
  | { type: "job"; data: Partial<Job> & { job_id: string } }
  | { type: "entity"; data: { type: string; id: string | null; reason: string } };

export interface ProjectEvents {
  jobs: Job[];
  activeJobs: Job[];
  connected: boolean;
  /** Bumps whenever the server says an entity changed. Depend on it to refetch. */
  revision: number;
  refresh: () => Promise<void>;
}

export function useProjectEvents(projectId: string | null): ProjectEvents {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [connected, setConnected] = useState(false);
  const [revision, setRevision] = useState(0);
  const retry = useRef(0);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    try {
      setJobs((await jobsApi.list(projectId)).items);
    } catch {
      /* the stream will resync; a failed poll is not worth surfacing */
    }
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      source = new EventSource(`/api/v1/projects/${projectId}/events`);

      source.onopen = () => {
        setConnected(true);
        retry.current = 0;
        // Always resync on (re)connect: SSE gaps are inevitable, and the
        // stream only tells us about changes it saw us connected for.
        void refresh();
      };

      const onJob = (e: MessageEvent) => {
        const data = JSON.parse(e.data) as ServerEvent["data"] & { job_id: string };
        setJobs((prev) => {
          const i = prev.findIndex((j) => j.id === data.job_id);
          if (i === -1) {
            void refresh();          // a job we have not seen before
            return prev;
          }
          const next = [...prev];
          next[i] = { ...next[i], ...data, id: data.job_id } as Job;
          return next;
        });
      };

      source.addEventListener("job", onJob as EventListener);
      source.addEventListener("entity", () => setRevision((r) => r + 1));

      source.onerror = () => {
        setConnected(false);
        source?.close();
        if (cancelled) return;
        // Back off so a server restart does not become a reconnect storm.
        const delay = Math.min(1000 * 2 ** retry.current++, 15000);
        reconnectTimer = setTimeout(connect, delay);
      };
    };

    void refresh();
    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      source?.close();
    };
  }, [projectId, refresh]);

  const activeJobs = jobs.filter((j) => ACTIVE_JOB_STATUSES.includes(j.status));
  return { jobs, activeJobs, connected, revision, refresh };
}

/** Resolve a single job to a terminal state, for buttons that must wait. */
export function useJobWatcher() {
  return useCallback(async (jobId: string, onTick?: (j: Job) => void) => {
    for (;;) {
      const job = await jobsApi.get(jobId);
      onTick?.(job);
      if (!ACTIVE_JOB_STATUSES.includes(job.status)) return job;
      await new Promise((r) => setTimeout(r, 1200));
    }
  }, []);
}
