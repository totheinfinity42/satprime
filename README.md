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

SatePrime implements a performance optimization framework for containerized satellite applications through two phases, located in the `satecode/` directory. It follows a profile–synthesize–recover design that moves complex, non-real-time analysis to the ground and enables fast recovery onboard.

The Ground-Side Construction Phase, deployed on the ground, generates an optimized and recoverable container image for a given application. It first performs application-state profiling: an injected entrypoint wrapper statically analyzes the application's imports, preloads all dependencies, and suspends itself at a stable recovery point after dependency loading and before input-specific computation, at which point the reusable process state is captured through process checkpointing (Step 1). It then performs boot-and-recovery access profiling by replaying the recovery process with I/O tracing to derive the file access order along the startup path, partitioning files into a startup-critical foreground tier and a deferrable background tier (Step 2). Finally, it performs recoverable image synthesis: the original root file system is merged with the captured state and relaid out in access order, then packed into a read-only EROFS image with an embedded heat boundary (Step 3).
The main implementation is provided in the files `agent.py`, `runtime.py`, and `monitor.py`.

The Satellite-Side Execution Phase, residing in the satellite system, restores and runs the application from the optimized image instead of starting from scratch. Once the computing payload is powered on, it prefetches the heat-boundary bytes of the image to warm the page cache during system startup, then restores the application from the recovery point and resumes execution directly (Step 4). In addition, it integrates a reliability fallback mechanism that checks recovery before, during, and after restoration and automatically reverts to the original cold-start path in case of recovery failures (Step 5).
The main implementation is provided in the file `codegen.py`.

Other code
 - We provide a command-line entry point that exposes the above stages as composable subcommands (`patch`, `snapshot`, `record`, `build`, `emit`). (`cli.py` and `__main__.py`)
 - We provide shared configuration for image labels, environment keys, and file paths used across the pipeline. (`config.py`)


## Deployment

- Assets related to a deployment case are provided under the `deployment/` directory.

## Usage

todo