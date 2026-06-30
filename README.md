# SatePrime: Performance Optimization of Satellite Applications in Satellite Computing

## Evaluated Satellite Applications

 - The evaluation suite comprises 10 typical satellite on-orbit applications, labeled app1 to app10, under the `apps/` directory.
 - Each application is accompanied by its code repository and a Dockerfile tailored for the satellite payload environment.

    | AppID | Description |
    | ----- | ----------- |
    | App1  | Object Detection |
    | App2  | Image Classification |
    | App3  | Data Compression |
    | App4  | Semantic Segmentation |
    | App5  | Oriented Object Detection |
    | App6  | Rotated Object Detection |
    | App7  | Onboard Inference |
    | App8  | Cloud Detection |
    | App9  | Target Detection |
    | App10 | Model Deployment |

## Satellite Application Performance Optimization Code

The optimization framework operates through a coordinated multi-phase pipeline, implemented across modules under `satecode/`.

 - A static initialization preloader is injected into the execution artifact, constructing a transitive dependency closure and materializing it within the host process prior to application entry, thereby collapsing cold-path latency.
    - Core logic resides in `agent.py` and `runtime.py`.
 - A state-preserving suspension mechanism captures the live process image at a well-defined synchronization barrier, persisting the memory-resident representation for subsequent warm-path restoration.
    - Core logic resides in `monitor.py`.
 - An access-trace profiler instruments the artifact's initialization sequence, partitioning the file corpus into a latency-critical foreground tier and a deferrable background tier based on temporal proximity to launch completion.
    - Core logic resides in `monitor.py`.
 - A stratified artifact compiler consumes the tiered partitioning and emits a self-describing linearized image with an embedded heat boundary, allowing an external pre-warmer to stage critical blocks into the page cache before the runtime engine is invoked.
    - Core logic resides in `runtime.py` and `codegen.py`.
- Auxiliary tooling
    - A CLI entry point exposing the above stages as composable subcommands. (`cli.py`)


## Deployment

- Assets related to a deployment case are provided under the `deployment/` directory.

## Usage

todo