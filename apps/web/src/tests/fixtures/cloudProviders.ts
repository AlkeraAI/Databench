// The cloud providers the machine fixtures run on. The open platform names only the local
// box; the product registers these names during composition, so a test of a cloud machine
// registers them the same way. Import it for its effect, once per test file.

import { PROVIDER_LABELS } from "@alkera/ui";

PROVIDER_LABELS.register({ key: "ec2", label: "EC2" });
PROVIDER_LABELS.register({ key: "runpod", label: "RunPod" });
