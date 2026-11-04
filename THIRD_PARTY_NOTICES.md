# License scope and third-party notices

The [MIT License](LICENSE) covers this project's own source code, tests and documentation. It does not cover the material listed here.

## Not covered by this project's license

- **Videos and images.** The `.mp4` and `.jpg` files in `web/static/media/` are included to run and demonstrate OneShot. The builder's phone recordings remain all rights reserved.
- **Stock video.** `web/static/media/real_demo.mp4` and `real_seen.mp4` are derived from a [stock video by cottonbro studio](https://www.pexels.com/video/a-kid-playing-with-wooden-building-blocks-3676968/) on Pexels, used under the Pexels License.
- **Dependencies.** Packages listed in `requirements.lock` aren't included in this repository and keep their own licenses. These include `meta-sam-parser` (SAM License) and `robot_descriptions`, which downloads the Franka Panda model from MuJoCo Menagerie (Apache-2.0) at runtime.
- **Meta models.** Muse Spark, Muse Spark Open, Muse Voice Transcribe and SAM 3.1 are accessed as services and are subject to their own terms.

## Code adapted from Meta's cookbook

These files adapt recipes `07_sam/01_api_basics` and `03_use_cases/14_muse_robotics` from [meta-model-cookbook](https://github.com/meta-models/meta-model-cookbook):

- `probes/p1_sam_video.py`
- `oneshot/evidence.py`, which was adapted from that probe
- `oneshot/sim/`

The adapted portions remain subject to the cookbook's license:

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
