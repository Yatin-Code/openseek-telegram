# OpenSeek

OpenSeek lets Nuvio clients exchange reusable NVMT preview packages through a shared pool.

## Language

**NVMT package**:
A versioned collection of time-indexed preview tiles that a Nuvio client can use while playing a video.

**Shared pool**:
The collection of published NVMT packages and matching hints that participating OpenSeek nodes make available to Nuvio clients.

**Pool node**:
An OpenSeek deployment that indexes the shared pool and serves package requests.

**Uploader**:
A Nuvio client or an OpenSeek owner action that submits an NVMT package and its hints to the shared pool.

**Receiver**:
A Nuvio client requesting an exact or hint-matched NVMT package for playback.

**Giver**:
A pool node that serves an NVMT package to a receiver.

**Admin generation**:
An owner-started task that creates an NVMT package from the dashboard, separate from Nuvio's playback-time generation.
