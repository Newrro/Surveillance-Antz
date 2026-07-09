# Research — Robust multi-camera person identity (anti-fragmentation)

> Deep-research synthesis — 106 agents, web sources adversarially verified (2/3 vote). Scoped to this repo: RTX 4050 6 GB, ~13 RTSP cams, RT-DETR + AdaFace(+MTCNN) + OSNet + Qdrant + FastAPI. Basis for the Phase 1–3 identity upgrade.

## Executive summary

To stop the same person fragmenting into multiple "Unknown" identities, the industry pattern is a three-tier hierarchy — a within-camera appearance-aware tracker, tracklet-level (not per-frame) identity resolution using quality-selected best shots, and a cross-camera fusion stage that clusters re-ID embeddings under spatio-temporal (camera-topology + travel-time) gating. For the user's 6 GB / 13-camera edge stack, the highest-leverage first fix is to replace the IoU-only tracker with an appearance-embedding tracker (BoT-SORT or Deep OC-SORT via the BoxMOT library), which cuts ID switches roughly in half and stops broken tracks from minting new Unknowns; Deep OC-SORT reports 1,023 ID switches on MOT17-test vs ByteTrack's 2,196, and BoT-SORT-ReID reaches IDF1 80.2. Face remains the primary identity signal (AdaFace, whose feature-norm is itself a built-in quality proxy), with best-frame selection per tracklet via a quality measure (CR-FIQA or SER-FIQ) before embedding, and body re-ID used only as a constrained fallback. Cross-camera merging should mirror NVIDIA Metropolis and the AI City Challenge winners: emit one re-ID embedding per single-camera tracklet, then agglomerative/hierarchical clustering refined by Hungarian reassignment, with a camera-topology/travel-time gate restricting the candidate gallery to plausibly-reachable cameras and time windows so strangers in similar clothing are never merged. OSNet can be upgraded toward TransReID for a large accuracy gain if VRAM allows, but the fragmentation fix is primarily architectural (tracker + tracklet-level resolution + gated clustering), not a bigger body model.


## Findings — 7, ranked (verified with evidence + sources)

### 1. Replace the IoU-only within-camera tracker with an appearance-embedding tracker (BoT-SORT or Deep OC-SORT) via the BoxMOT library — this is the single highest-leverage fix to stop broken tracks from minting new Unknown identities. Appearance-aware trackers substantially beat motion-only ones on identity metrics and roughly halve ID switches.

confidence: **high** · verify: 3-0 (all constituent claims unanimous)

BoxMOT provides swappable SOTA trackers (BoT-SORT, StrongSORT, DeepOCSORT, ByteTrack, OC-SORT, HybridSORT, BoostTrack) sharing one detector + re-ID embedding pipeline, so the user can drop it onto their existing RT-DETR detector. On MOT17: BoT-SORT reaches IDF1 81.94 / HOTA 69.44 vs motion-only ByteTrack IDF1 79.16 / HOTA 67.68 and OC-SORT IDF1 77.90 / HOTA 66.44. Deep OC-SORT cuts ID switches to 1,023 on MOT17-test vs OC-SORT's 1,950 (a 47.5% reduction) and ByteTrack's 2,196 (53.4%), while topping IDF1 (80.6) and HOTA (64.9). BoT-SORT combines motion + appearance with camera-motion compensation and a more accurate Kalman state vector; BoT-SORT-ReID hit MOTA 80.5 / IDF1 80.2 / HOTA 65.0, the first tracker above IDF1 80. Deep OC-SORT is well-suited to edge/noisy footage because it modulates the appearance-embedding EMA weight per-frame by detector confidence, rejecting occluded/blurred embeddings so only high-quality features update a track — directly relevant to face-less/occluded angles that currently fragment tracks.

- https://github.com/mikel-brostrom/boxmot
- https://arxiv.org/pdf/2302.11813
- https://arxiv.org/pdf/2206.14651

### 2. Resolve identity at the tracklet level, not per-frame: pool features across a track and use a face-image-quality measure to select the best shot(s) before embedding. Rejecting low-quality frames directly and measurably improves recognition accuracy.

confidence: **high** · verify: 3-0 (claim 5 was 2-1; quality/FIQA claims 19-23 unanimous)

The MTMCT literature re-links fragmented tracks via tracklet-level association using a fused/pooled tracking feature rather than per-frame matching (State-aware Re-ID, CVPR 2019 AIC workshop). Quality-based frame filtering is proven via Error-vs-Reject curves: rejecting the lowest-quality samples causes verification error (FNMR at fixed FMR) to drop rapidly, a result standardized in NIST IR 7544 / ISO-IEC 29794-1. Two decoupled quality estimators fit the stack: CR-FIQA (ResNet-50 'S' or ResNet-100 'L') outperforms SER-FIQ, MagFace and SDD-FIQA by significant margins across 8 benchmarks / 4 FR models and is a pure cross-model quality predictor pairable with any FR model (ArcFace, AdaFace, etc.); SER-FIQ needs no training or labels and reuses the deployed FR model via m stochastic dropout forward passes (quality = sigmoid of negative mean pairwise embedding distance), and its 'same-model' variant beats all baselines by a large margin but requires the recognition model to have dropout. Practically: run the tracker, score each face crop's quality, embed only top-k shots per tracklet, and aggregate.

- https://arxiv.org/pdf/1906.01357
- https://openaccess.thecvf.com/content/CVPR2023/papers/Boutros_CR-FIQA_Face_Image_Quality_Assessment_by_Learning_Sample_Relative_Classifiability_CVPR_2023_paper.pdf
- https://arxiv.org/pdf/2003.09373

### 3. Keep AdaFace as the primary face embedder — its feature norm is itself a built-in image-quality proxy, so it self-regulates on low-quality faces and needs no separate quality module for the recognition margin. This is well-matched to blurry/off-angle surveillance faces.

confidence: **high** · verify: 3-0

AdaFace (CVPR 2022) uses feature norm as a proxy for face image quality (correlated 0.5235 with BRISQUE) to adapt the recognition margin, with no separate quality-assessment module. Its quality-adaptive margin de-emphasizes unidentifiable low-quality images while emphasizing hard samples only in high-quality images, avoiding the failure mode where hard-sample mining fixates on unidentifiable faces. This means AdaFace already discounts poor face crops internally; a separate FIQA (CR-FIQA/SER-FIQ) is still worth adding for the orthogonal job of best-frame SELECTION per tracklet (which shots to embed), not margin adaptation.

- https://ar5iv.labs.arxiv.org/html/2204.00964

### 4. Structure cross-camera identity as a decoupled fusion stage: emit one re-ID embedding per single-camera tracklet, then consolidate into global IDs with hierarchical/agglomerative clustering refined by iterative Hungarian reassignment. This is the NVIDIA Metropolis / AI City Challenge reference pattern and maps cleanly onto the user's Qdrant + Postgres + FastAPI brain.

confidence: **high** · verify: 3-0 (claims 0,1,3,6,7 all unanimous)

NVIDIA Metropolis MTMC decouples per-camera Perception (bounding boxes + single-camera trajectories + Re-ID embedding vectors passed as metadata over Kafka, not raw frames) from a Multi-Camera Fusion microservice that does two-step hierarchical clustering + an ID Merging module to consolidate per-camera IDs into global IDs, then Behavior Analytics downstream. The clustering fuses re-ID appearance features with spatio-temporal info and refines clusters by iterative Hungarian matching. The AI City Challenge winner (arXiv 2105.01213) jointly applies a camera-link model with hierarchical clustering for global ID assignment. A single CNN appearance feature can serve both within-camera tracking and cross-camera re-ID (Ristani & Tomasi, CVPR 2018), so the same embedding the tracker uses can feed the fusion clustering. For the user, this means: store per-tracklet embeddings in Qdrant, run periodic constrained agglomerative clustering to assign/merge global identities, and stop resolving identity frame-by-frame.

- https://docs.nvidia.com/mms/text/MDX_Multi_Camera_Tracking_MS_Overview.html
- https://developer.nvidia.com/blog/real-time-vision-ai-from-digital-twins-to-cloud-native-deployment-with-nvidia-metropolis-microservices-and-nvidia-isaac-sim/
- https://arxiv.org/pdf/2105.01213
- https://arxiv.org/abs/1803.10859

### 5. Gate every cross-camera and body-re-ID merge by camera topology and travel-time (spatio-temporal constraints) so that only cameras a person could plausibly have moved between, within a valid transition-time window, are candidates. This is the mechanism that prevents merging strangers in similar clothing while still re-linking one person's fragmented sightings.

confidence: **high** · verify: 3-0 (claims 2,3,17,18 all unanimous)

A trajectory-based camera-link model built from camera topology reduces the re-ID candidate search space and improves MTMCT; combined with hierarchical clustering it yields global IDs (arXiv 2105.01213). STFN with Causal Identity Matching (arXiv 2408.05558, 2024) estimates camera-network topology via an adaptive Parzen window over pairwise transition times and dynamically restricts the gallery set to topology-adjacent cameras within valid time windows — the paper states this dynamic gallery restriction is precisely what reduces spurious cross-camera matches (up to 99.70% rank-1 / 95.5% mAP on benchmarks). Actionable recipe: for the user's 13 cameras, encode an adjacency graph + observed travel-time distributions, and only admit merge candidates from reachable cameras within the plausible time window; this is the safety constraint that lets body re-ID be used at all without false merges.

- https://arxiv.org/pdf/2105.01213
- https://arxiv.org/pdf/2408.05558

### 6. Use body/clothing re-ID only as a CONSTRAINED fallback to re-link fragmented sightings — restricted to same/adjacent camera + short time window + high similarity threshold — with face as the authoritative signal. Embedding occlusion/orientation/pose state into the re-ID feature further combats fragmentation.

confidence: **medium** · verify: 3-0 for the state-aware mechanism (claim 4); 2-1 for tracklet-level re-linking (claim 5)

The State-aware Re-ID work (CVPR 2019 AIC workshop, arXiv 1906.01357) shows that directly applying a body Re-ID model in MTMCT causes identity switches and tracklet fragmentation from occlusion/viewpoint change, and mitigates it by embedding occlusion-status, orientation and human-pose ('state-aware') information into the feature, then doing tracklet association with the fused feature. This validates the user's instinct that body/clothing must never merge people on its own (matching the repo's 'face-only identity' commit); body re-ID is safe only inside the spatio-temporal gate (Finding on topology/travel-time gating). The specific numeric thresholds/time-windows practitioners use were not established by a verified primary source in this research — see open questions and caveats.

- https://arxiv.org/pdf/1906.01357
- https://arxiv.org/pdf/2408.05558

### 7. OSNet body re-ID can be upgraded to a transformer backbone (TransReID) for a large accuracy gain, but the fragmentation fix is primarily architectural, not a bigger body model. TransReID (ViT-B/16, 256x128) reaches 88.9% mAP / 95.2% Rank-1 on Market-1501 and 67.4% mAP / 85.3% Rank-1 on MSMT17 vs OSNet's 52.9% mAP / 78.7% Rank-1 on MSMT17.

confidence: **medium** · verify: 3-0 for the benchmark claim (14); hardware verdict is synthesis-level

TransReID (ICCV 2021) outperforms CNN re-ID (including OSNet) by a large margin — a +14.5 mAP MSMT17 gap over OSNet. This is a valid upgrade path above the user's OSNet fallback, though newer methods (CLIP-ReID, SOLIDER) now exceed it. Caveat for the 6 GB / 13-camera constraint: a ViT-B backbone is heavier than OSNet, and with ~13 concurrent 1440p streams sharing one RTX 4050, VRAM and latency budget likely favor keeping a lightweight body model (OSNet or a small FastReID model) and investing the fix budget in the tracker + gated clustering instead. Body re-ID accuracy matters less once cross-camera merges are topology/time gated and face-primary. FastReID/BoxMOT provide swappable re-ID backbones to test this tradeoff empirically. Confidence is medium because the VRAM/latency verdict for TransReID on this specific hardware was reasoned from the constraint, not measured.

- https://openaccess.thecvf.com/content/ICCV2021/papers/He_TransReID_Transformer-Based_Object_Re-Identification_ICCV_2021_paper.pdf
- https://github.com/mikel-brostrom/boxmot


## caveats

Time-sensitivity: tracker and re-ID benchmarks are a fast-moving field. BoT-SORT/Deep OC-SORT numbers are 2022-2023 SOTA and are strong baselines in 2026, but the specific 'state-of-the-art' framing for BoT-SORT was explicitly refuted by adversarial voting (0-3) — treat it as a strong, well-supported choice, not the current leader. TransReID (2021) has been surpassed by CLIP-ReID and SOLIDER, which were named in the question but did NOT surface verified benchmark claims in this research; their exact Market-1501/MSMT17 numbers and VRAM footprints remain unverified here.

Hardware fit is largely un-measured: no verified source benchmarked any of these models on an RTX 4050 (6 GB) driving ~13 concurrent 1440p RTSP streams. Claims about which model 'fits' the VRAM/latency budget (especially TransReID vs OSNet) are engineering inferences from the constraint, not measured results — they need a local profiling pass. The user's own memory notes (grid=pipeline shm, SAM2 off live, gate-only identify, NVDEC) are the real budget authority.

Domain transfer: two camera-topology/link-model sources (arXiv 2105.01213, and partly 2408.05558) were developed on vehicle re-ID (AI City Challenge / VeRi). The spatio-temporal gating PRINCIPLE transfers directly to persons and is corroborated by the NVIDIA person-MTMC docs, but person-specific travel-time distributions must be learned from the user's own 13-camera deployment.

Missing quantitative thresholds: the research did NOT surface a verified primary source giving the concrete body-re-ID cosine-similarity thresholds and time-window lengths practitioners actually use for constrained merging. Those must be tuned empirically on the user's data — start strict (high similarity, short window, same/adjacent camera only) and loosen while watching false-merge rate.

Vendor-doc reliance: several cross-camera architecture claims rest on NVIDIA Metropolis documentation. These are descriptive of NVIDIA's own design (appropriate as an architecture reference) rather than independent performance evaluations, so treat Metropolis as a pattern to borrow, not a benchmarked guarantee.

Not covered by verified claims: FAISS/Milvus/Qdrant scaling specifics, gallery hygiene mechanics (temporal decay, quality-weighted centroids, dedup/merge), explicit quality-weighted face+body score-fusion decision rules, k-reciprocal re-ranking numbers, and the prosumer stacks (Frigate/Double Take/CompreFace) — these parts of the question did not produce claims that survived verification.
