import cv2
import json
import time
from pathlib import Path

import numpy as np
from rtmlib import RTMPose3d
from pythonosc.udp_client import SimpleUDPClient


# ============================================================
# SETTINGS
# ============================================================

CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

OSC_IP = "127.0.0.1"
OSC_PORT = 8000



# ------------------------------------------------------------
# Person detection
# ------------------------------------------------------------

# この秒数、立ち位置にいたらSTART
ENTER_SECONDS = 2.0

# 一瞬見失っただけではSTOPしない
EXIT_SECONDS = 0.7


# ------------------------------------------------------------
# Pose confidence
# ------------------------------------------------------------

JOINT_THRESHOLD = 0.35

PERSON_MEAN_SCORE_THRESHOLD = 0.40
PERSON_MIN_VISIBLE_JOINTS = 8


# ------------------------------------------------------------
# Arm
# ------------------------------------------------------------

# 1.0に近いほど肘が伸びている
MIN_EXTENSION = 0.72

# 2D上で肩と手首が近すぎる場合は方向判定しない
MIN_ARM_2D_PIXELS = 18.0


# ------------------------------------------------------------
# Calibration
# ------------------------------------------------------------

# 16 = 22.5°刻み
CALIBRATION_DIRECTIONS = 8

# 各方向で何フレーム平均するか
CALIBRATION_SAMPLES = 8

CALIBRATION_MIN_EXTENSION = 0.72

# キャリブレーション軌道から
# あまりにも離れた腕方向を無効にする
AZIMUTH_MAX_MAPPING_ERROR = 0.40

# キャリブレーションで次の方向へ移る猶予
CALIBRATION_PREPARE_SECONDS = 3.0

# 最初に立ち位置まで移動する時間
CALIBRATION_START_DELAY = 5.0
# ------------------------------------------------------------
# Smoothing
# ------------------------------------------------------------

# 水平方向
AZIMUTH_ALPHA = 0.25

# 上下方向は3Dなので強めに平滑化
ELEVATION_ALPHA = 0.15


# ------------------------------------------------------------
# Performance
# ------------------------------------------------------------

# 1 = 毎フレーム
# 重かったら 2
PROCESS_EVERY_N_FRAMES = 1

SHOW_PREVIEW = True


# ------------------------------------------------------------
# Elevation adjustment
# ------------------------------------------------------------

# 上下が逆だった場合 -1.0 にする
ELEVATION_SIGN = 1.0

# 必要なら角度オフセット
ELEVATION_OFFSET = 0.0


# ------------------------------------------------------------
# Calibration file
# ------------------------------------------------------------

CALIBRATION_FILE = Path("calibration.json")


# ============================================================
# RTMW3D MODEL
# ============================================================

RTMW3D_MODEL = (
    "https://huggingface.co/Soykaf/RTMW3D-x/resolve/main/onnx/"
    "rtmw3d-x_8xb64_cocktail14-384x288-b0a0eab7_20240626.onnx"
)

MODEL_INPUT_SIZE = (288, 384)


# ============================================================
# COCO BODY INDICES
# ============================================================

LEFT_SHOULDER = 5
RIGHT_SHOULDER = 6

LEFT_ELBOW = 7
RIGHT_ELBOW = 8

LEFT_WRIST = 9
RIGHT_WRIST = 10

LEFT_HIP = 11
RIGHT_HIP = 12

LEFT_KNEE = 13
RIGHT_KNEE = 14

LEFT_ANKLE = 15
RIGHT_ANKLE = 16


CORE_JOINTS = [
    LEFT_SHOULDER,
    RIGHT_SHOULDER,

    LEFT_ELBOW,
    RIGHT_ELBOW,

    LEFT_WRIST,
    RIGHT_WRIST,

    LEFT_HIP,
    RIGHT_HIP,

    LEFT_KNEE,
    RIGHT_KNEE,

    LEFT_ANKLE,
    RIGHT_ANKLE,
]


BODY_LINES = [
    (LEFT_SHOULDER, RIGHT_SHOULDER),

    (LEFT_SHOULDER, LEFT_ELBOW),
    (LEFT_ELBOW, LEFT_WRIST),

    (RIGHT_SHOULDER, RIGHT_ELBOW),
    (RIGHT_ELBOW, RIGHT_WRIST),

    (LEFT_SHOULDER, LEFT_HIP),
    (RIGHT_SHOULDER, RIGHT_HIP),

    (LEFT_HIP, RIGHT_HIP),

    (LEFT_HIP, LEFT_KNEE),
    (LEFT_KNEE, LEFT_ANKLE),

    (RIGHT_HIP, RIGHT_KNEE),
    (RIGHT_KNEE, RIGHT_ANKLE),
]


# ============================================================
# BASIC FUNCTIONS
# ============================================================

def normalize(v):

    v = np.asarray(
        v,
        dtype=np.float32
    )

    length = np.linalg.norm(v)

    if length < 1e-6:
        return None

    return v / length


def smooth_angle(
    previous,
    current,
    alpha
):

    """
    359° → 0° でも
    359 → 180 → 0 のようにならない角度補間
    """

    if previous is None:
        return float(
            current % 360.0
        )

    delta = (
        (
            current
            - previous
            + 180.0
        )
        % 360.0
    ) - 180.0

    result = (
        previous
        + alpha * delta
    )

    return float(
        result % 360.0
    )


def smooth_scalar(
    previous,
    current,
    alpha
):

    if previous is None:
        return float(current)

    return float(
        (1.0 - alpha) * previous
        + alpha * current
    )


def arm_indices(side):

    if side == "right":

        return (
            RIGHT_SHOULDER,
            RIGHT_ELBOW,
            RIGHT_WRIST
        )

    return (
        LEFT_SHOULDER,
        LEFT_ELBOW,
        LEFT_WRIST
    )


# ============================================================
# ARM 2D
# ============================================================

def arm_extension_2d(
    shoulder,
    elbow,
    wrist
):

    """
    肩---肘---手首が一直線なら約1.0
    """

    straight = np.linalg.norm(
        wrist - shoulder
    )

    upper = np.linalg.norm(
        elbow - shoulder
    )

    lower = np.linalg.norm(
        wrist - elbow
    )

    total = upper + lower

    if total < 1e-6:
        return 0.0

    return float(
        np.clip(
            straight / total,
            0.0,
            1.0
        )
    )


def get_arm_2d(
    kpts2d,
    scores,
    side
):

    shoulder_idx, elbow_idx, wrist_idx = arm_indices(
        side
    )

    confidence = float(
        min(
            scores[shoulder_idx],
            scores[elbow_idx],
            scores[wrist_idx]
        )
    )

    if confidence < JOINT_THRESHOLD:
        return None


    shoulder = np.asarray(
        kpts2d[shoulder_idx],
        dtype=np.float32
    )

    elbow = np.asarray(
        kpts2d[elbow_idx],
        dtype=np.float32
    )

    wrist = np.asarray(
        kpts2d[wrist_idx],
        dtype=np.float32
    )


    vector = wrist - shoulder

    length_px = float(
        np.linalg.norm(vector)
    )

    direction = normalize(
        vector
    )

    if direction is None:
        return None


    extension = arm_extension_2d(
        shoulder,
        elbow,
        wrist
    )


    return {
        "direction": direction,
        "length_px": length_px,
        "extension": extension,
        "confidence": confidence
    }


# ============================================================
# 3D / ELEVATION
# ============================================================

def simcc_to_normalized_xyz(
    kpts_simcc
):

    """
    RTMPose3d SimCC座標を
    xyzで比較できる程度に正規化する。
    """

    pts = np.asarray(
        kpts_simcc,
        dtype=np.float32
    ).copy()

    pts[:, 0] /= float(
        MODEL_INPUT_SIZE[0]
    )

    pts[:, 1] /= float(
        MODEL_INPUT_SIZE[1]
    )

    # zも高さ側のスケールに合わせる
    pts[:, 2] /= float(
        MODEL_INPUT_SIZE[1]
    )

    return pts


def get_body_up(
    kpts_simcc,
    scores
):

    needed = [
        LEFT_SHOULDER,
        RIGHT_SHOULDER,
        LEFT_HIP,
        RIGHT_HIP
    ]


    for i in needed:

        if scores[i] < JOINT_THRESHOLD:
            return None


    pts = simcc_to_normalized_xyz(
        kpts_simcc
    )


    shoulder_center = (
        pts[LEFT_SHOULDER]
        + pts[RIGHT_SHOULDER]
    ) / 2.0


    hip_center = (
        pts[LEFT_HIP]
        + pts[RIGHT_HIP]
    ) / 2.0


    # 腰 → 肩
    up = (
        shoulder_center
        - hip_center
    )


    return normalize(
        up
    )


def calculate_elevation(
    kpts_simcc,
    scores,
    side,
    room_up
):

    if room_up is None:
        return None


    shoulder_idx, elbow_idx, wrist_idx = arm_indices(
        side
    )


    confidence = float(
        min(
            scores[shoulder_idx],
            scores[elbow_idx],
            scores[wrist_idx]
        )
    )


    if confidence < JOINT_THRESHOLD:
        return None


    pts = simcc_to_normalized_xyz(
        kpts_simcc
    )


    shoulder = pts[
        shoulder_idx
    ]

    wrist = pts[
        wrist_idx
    ]


    arm = (
        wrist
        - shoulder
    )


    if np.linalg.norm(arm) < 1e-6:
        return None


    # --------------------------------------------------------
    # room_up方向の成分
    # --------------------------------------------------------

    vertical = float(
        np.dot(
            arm,
            room_up
        )
    )


    # --------------------------------------------------------
    # 水平面成分
    # --------------------------------------------------------

    horizontal_vec = (
        arm
        - vertical * room_up
    )


    horizontal = float(
        np.linalg.norm(
            horizontal_vec
        )
    )


    elevation = np.degrees(
        np.arctan2(
            vertical,
            horizontal
        )
    )


    elevation = (
        ELEVATION_SIGN
        * elevation
        + ELEVATION_OFFSET
    )


    elevation = float(
        np.clip(
            elevation,
            -90.0,
            90.0
        )
    )


    return elevation


# ============================================================
# PERSON POSITION
# ============================================================

def person_inside_standing_area(
    kpts2d,
    scores,
    standing_roi
):

    x, y, w, h = standing_roi


    core_scores = np.array(
        [
            scores[i]
            for i in CORE_JOINTS
        ],
        dtype=np.float32
    )


    visible_count = int(
        np.sum(
            core_scores
            > JOINT_THRESHOLD
        )
    )


    mean_score = float(
        np.mean(
            core_scores
        )
    )


    if (
        visible_count
        < PERSON_MIN_VISIBLE_JOINTS
    ):
        return False


    if (
        mean_score
        < PERSON_MEAN_SCORE_THRESHOLD
    ):
        return False


    # 両足首が必要
    if (
        scores[LEFT_ANKLE]
        < JOINT_THRESHOLD
        or
        scores[RIGHT_ANKLE]
        < JOINT_THRESHOLD
    ):
        return False


    left_ankle = np.asarray(
        kpts2d[LEFT_ANKLE],
        dtype=np.float32
    )

    right_ankle = np.asarray(
        kpts2d[RIGHT_ANKLE],
        dtype=np.float32
    )


    foot_center = (
        left_ankle
        + right_ankle
    ) / 2.0


    fx = float(
        foot_center[0]
    )

    fy = float(
        foot_center[1]
    )


    return (
        x <= fx <= x + w
        and
        y <= fy <= y + h
    )


# ============================================================
# CALIBRATION → AZIMUTH
# ============================================================

def azimuth_from_calibration(
    current_direction,
    reference_vectors
):

    """
    保存された16方向の2Dベクトルから
    現在の物理的な0～360°を求める。

    隣り合うキャリブレーション点を結ぶ線分のうち、
    現在ベクトルに最も近い場所を採用する。
    """

    current = normalize(
        current_direction
    )


    if current is None:
        return None, 999.0


    refs = np.asarray(
        reference_vectors,
        dtype=np.float32
    )


    n = len(refs)

    if n < 3:
        return None, 999.0


    step = (
        360.0 / n
    )


    best_error = float("inf")
    best_angle = None


    for i in range(n):

        a = refs[i]

        b = refs[
            (i + 1) % n
        ]


        segment = (
            b - a
        )


        denom = float(
            np.dot(
                segment,
                segment
            )
        )


        if denom < 1e-8:

            t = 0.0
            projected = a

        else:

            t = float(
                np.clip(
                    np.dot(
                        current - a,
                        segment
                    )
                    / denom,
                    0.0,
                    1.0
                )
            )


            projected = (
                a
                + t * segment
            )


        error = float(
            np.linalg.norm(
                current
                - projected
            )
        )


        if error < best_error:

            best_error = error

            physical_angle = (
                i * step
                + t * step
            )


            best_angle = (
                physical_angle
                % 360.0
            )


    return (
        best_angle,
        best_error
    )


# ============================================================
# DRAW
# ============================================================

def draw_body(
    image,
    kpts2d,
    scores
):

    for a, b in BODY_LINES:

        if (
            scores[a]
            < JOINT_THRESHOLD
            or
            scores[b]
            < JOINT_THRESHOLD
        ):
            continue


        p1 = (
            int(kpts2d[a][0]),
            int(kpts2d[a][1])
        )

        p2 = (
            int(kpts2d[b][0]),
            int(kpts2d[b][1])
        )


        cv2.line(
            image,
            p1,
            p2,
            (255, 255, 255),
            2
        )


    for i in CORE_JOINTS:

        if (
            scores[i]
            < JOINT_THRESHOLD
        ):
            continue


        p = (
            int(kpts2d[i][0]),
            int(kpts2d[i][1])
        )


        cv2.circle(
            image,
            p,
            3,
            (255, 255, 255),
            -1
        )


def draw_standing_area(
    image,
    standing_roi
):

    x, y, w, h = standing_roi


    cv2.rectangle(
        image,
        (x, y),
        (x + w, y + h),
        (0, 255, 255),
        2
    )


# ============================================================
# RTMW3D
# ============================================================

def infer_pose(
    pose_model,
    crop
):

    try:

        (
            keypoints3d,
            scores,
            keypoints_simcc,
            keypoints2d
        ) = pose_model(
            crop
        )


        if len(keypoints2d) == 0:
            return None


        return {
            "keypoints3d":
                keypoints3d[0],

            "scores":
                scores[0],

            "simcc":
                keypoints_simcc[0],

            "keypoints2d":
                keypoints2d[0]
        }


    except Exception as e:

        print(
            "Pose error:",
            e
        )

        return None


# ============================================================
# SAVE / LOAD
# ============================================================

def save_calibration(
    data
):

    with CALIBRATION_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


    print(
        "Calibration saved:",
        CALIBRATION_FILE.resolve()
    )


def load_calibration():

    if not CALIBRATION_FILE.exists():
        return None


    try:

        with CALIBRATION_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(
                f
            )


        if data.get("version") != 2:

            print(
                "Old calibration file."
            )

            return None


        if (
            len(
                data["right_vectors"]
            )
            != CALIBRATION_DIRECTIONS
        ):

            return None


        if (
            len(
                data["left_vectors"]
            )
            != CALIBRATION_DIRECTIONS
        ):

            return None


        return data


    except Exception as e:

        print(
            "Could not load calibration:",
            e
        )

        return None


# ============================================================
# ROI SELECTION
# ============================================================

def select_rois(
    cap
):

    success, frame = cap.read()


    if not success:

        raise RuntimeError(
            "Camera error"
        )


    # --------------------------------------------------------
    # Person Area
    # --------------------------------------------------------

    x, y, w, h = cv2.selectROI(
        "1. Select PERSON AREA",
        frame,
        showCrosshair=True,
        fromCenter=False
    )


    cv2.destroyWindow(
        "1. Select PERSON AREA"
    )


    person_roi = [
        int(x),
        int(y),
        int(w),
        int(h)
    ]


    if (
        person_roi[2] <= 0
        or
        person_roi[3] <= 0
    ):

        raise RuntimeError(
            "Person ROI was not selected."
        )


    px, py, pw, ph = person_roi


    crop = frame[
        py:py + ph,
        px:px + pw
    ].copy()


    # --------------------------------------------------------
    # Standing Area
    # --------------------------------------------------------

    x, y, w, h = cv2.selectROI(
        "2. Select STANDING AREA",
        crop,
        showCrosshair=True,
        fromCenter=False
    )


    cv2.destroyWindow(
        "2. Select STANDING AREA"
    )


    standing_roi = [
        int(x),
        int(y),
        int(w),
        int(h)
    ]


    if (
        standing_roi[2] <= 0
        or
        standing_roi[3] <= 0
    ):

        raise RuntimeError(
            "Standing Area was not selected."
        )


    return (
        person_roi,
        standing_roi
    )


# ============================================================
# CAPTURE ONE CALIBRATION DIRECTION
# ============================================================

def capture_calibration_direction(
    cap,
    pose_model,
    person_roi,
    standing_roi,
    side,
    angle
):

    px, py, pw, ph = person_roi

    # ========================================================
    # 3秒間、その方向へ腕を動かす時間
    # ========================================================

    start_time = time.monotonic()

    while True:

        success, frame = cap.read()

        if not success:
            continue

        crop = frame[
            py:py + ph,
            px:px + pw
        ]

        pose = infer_pose(
            pose_model,
            crop
        )

        preview = crop.copy()

        draw_standing_area(
            preview,
            standing_roi
        )

        ready = False

        if pose is not None:

            k2d = pose["keypoints2d"]
            scores = pose["scores"]

            draw_body(
                preview,
                k2d,
                scores
            )

            arm = get_arm_2d(
                k2d,
                scores,
                side
            )

            if (
                person_inside_standing_area(
                    k2d,
                    scores,
                    standing_roi
                )
                and
                arm is not None
                and
                arm["length_px"] >= MIN_ARM_2D_PIXELS
                and
                arm["extension"] >= CALIBRATION_MIN_EXTENSION
            ):
                ready = True

        elapsed = (
            time.monotonic()
            - start_time
        )

        remaining = max(
            0.0,
            CALIBRATION_PREPARE_SECONDS
            - elapsed
        )

        # ====================================================
        # 表示
        # ====================================================

        cv2.putText(
            preview,
            f"{side.upper()}  {angle:.1f} deg",
            (15, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2
        )

        cv2.putText(
            preview,
            f"Hold in {remaining:.1f}",
            (15, 65),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 255),
            2
        )

        cv2.putText(
            preview,
            "READY" if ready else "Extend arm",
            (15, 100),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (
                (0, 255, 0)
                if ready
                else
                (0, 0, 255)
            ),
            2
        )

        cv2.imshow(
            "Calibration",
            preview
        )

        cv2.waitKey(1)

        # 3秒経過
        if elapsed >= CALIBRATION_PREPARE_SECONDS:

            # ポーズがまだ条件を満たしていないなら
            # 自動的に待つ
            if ready:
                break


    # ========================================================
    # 自動サンプリング
    # ========================================================

    samples = []

    while len(samples) < CALIBRATION_SAMPLES:

        success, frame = cap.read()

        if not success:
            continue

        crop = frame[
            py:py + ph,
            px:px + pw
        ]

        pose = infer_pose(
            pose_model,
            crop
        )

        preview = crop.copy()

        draw_standing_area(
            preview,
            standing_roi
        )

        if pose is not None:

            k2d = pose["keypoints2d"]
            scores = pose["scores"]

            draw_body(
                preview,
                k2d,
                scores
            )

            arm = get_arm_2d(
                k2d,
                scores,
                side
            )

            if (
                person_inside_standing_area(
                    k2d,
                    scores,
                    standing_roi
                )
                and
                arm is not None
                and
                arm["length_px"] >= MIN_ARM_2D_PIXELS
                and
                arm["extension"] >= CALIBRATION_MIN_EXTENSION
            ):

                samples.append(
                    arm["direction"]
                )

        cv2.putText(
            preview,
            f"{side.upper()}  {angle:.1f} deg",
            (15, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2
        )

        cv2.putText(
            preview,
            f"RECORDING {len(samples)}/{CALIBRATION_SAMPLES}",
            (15, 65),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2
        )

        cv2.imshow(
            "Calibration",
            preview
        )

        cv2.waitKey(1)


    # ========================================================
    # 平均ベクトル
    # ========================================================

    mean_vector = normalize(
        np.mean(
            np.asarray(samples),
            axis=0
        )
    )

    if mean_vector is None:
        raise RuntimeError(
            "Calibration failed"
        )

    return mean_vector.tolist()

# ============================================================
# CALIBRATE 360
# ============================================================

def calibrate_directions(
    cap,
    pose_model,
    person_roi,
    standing_roi
):

    step = (
        360.0
        / CALIBRATION_DIRECTIONS
    )

    angles = [
        i * step
        for i in range(
            CALIBRATION_DIRECTIONS
        )
    ]

    right_vectors = []
    left_vectors = []

    print()
    print(
        "===== DIRECTION CALIBRATION ====="
    )
    print(
        "0 deg = SPATで0°にしたい実空間方向"
    )
    print(
        "表示された角度の方向へ腕を伸ばしてください"
    )
    print()

    # ========================================================
    # 最初に立ち位置まで移動する時間
    # ========================================================

    print(
        f"Calibration starts in "
        f"{CALIBRATION_START_DELAY} seconds."
    )

    start_wait = time.monotonic()

    while True:

        elapsed = (
            time.monotonic()
            - start_wait
        )

        remaining = (
            CALIBRATION_START_DELAY
            - elapsed
        )

        if remaining <= 0:
            break

        success, frame = cap.read()

        if not success:
            continue

        px, py, pw, ph = person_roi

        crop = frame[
            py:py + ph,
            px:px + pw
        ].copy()

        # 立ち位置表示
        draw_standing_area(
            crop,
            standing_roi
        )

        cv2.putText(
            crop,
            (
                f"Calibration starts in "
                f"{remaining:.1f}"
            ),
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 255),
            2
        )

        cv2.putText(
            crop,
            "Move to the standing area",
            (20, 75),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1
        )

        cv2.imshow(
            "Calibration",
            crop
        )

        cv2.waitKey(1)

    # ========================================================
    # RIGHT HAND
    # ========================================================

    print()
    print("----- RIGHT HAND -----")

    for angle in angles:

        print(
            f"RIGHT -> {angle:.1f} deg"
        )

        vector = capture_calibration_direction(
            cap,
            pose_model,
            person_roi,
            standing_roi,
            "right",
            angle
        )

        right_vectors.append(
            vector
        )

    # ========================================================
    # 左右切り替えのため少し待つ
    # ========================================================

    switch_seconds = 3.0

    switch_start = time.monotonic()

    while True:

        elapsed = (
            time.monotonic()
            - switch_start
        )

        remaining = (
            switch_seconds
            - elapsed
        )

        if remaining <= 0:
            break

        success, frame = cap.read()

        if not success:
            continue

        px, py, pw, ph = person_roi

        crop = frame[
            py:py + ph,
            px:px + pw
        ].copy()

        draw_standing_area(
            crop,
            standing_roi
        )

        cv2.putText(
            crop,
            "RIGHT COMPLETE",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2
        )

        cv2.putText(
            crop,
            (
                f"LEFT starts in "
                f"{remaining:.1f}"
            ),
            (20, 75),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 255),
            2
        )

        cv2.imshow(
            "Calibration",
            crop
        )

        cv2.waitKey(1)

    # ========================================================
    # LEFT HAND
    # ========================================================

    print()
    print("----- LEFT HAND -----")

    for angle in angles:

        print(
            f"LEFT -> {angle:.1f} deg"
        )

        vector = capture_calibration_direction(
            cap,
            pose_model,
            person_roi,
            standing_roi,
            "left",
            angle
        )

        left_vectors.append(
            vector
        )

    # ========================================================
    # 完了表示
    # ========================================================

    complete_start = time.monotonic()

    while (
        time.monotonic()
        - complete_start
        < 2.0
    ):

        success, frame = cap.read()

        if not success:
            continue

        px, py, pw, ph = person_roi

        crop = frame[
            py:py + ph,
            px:px + pw
        ].copy()

        cv2.putText(
            crop,
            "CALIBRATION COMPLETE",
            (20, 45),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2
        )

        cv2.imshow(
            "Calibration",
            crop
        )

        cv2.waitKey(1)

    cv2.destroyWindow(
        "Calibration"
    )

    print()
    print(
        "===== CALIBRATION COMPLETE ====="
    )
    print()

    return (
        right_vectors,
        left_vectors
    )

# ============================================================
# FULL CALIBRATION
# ============================================================

def run_full_calibration(
    cap,
    pose_model
):

    person_roi, standing_roi = select_rois(
        cap
    )


    right_vectors, left_vectors = calibrate_directions(
        cap,
        pose_model,
        person_roi,
        standing_roi
    )


    data = {

        "version": 2,

        "camera_width":
            CAMERA_WIDTH,

        "camera_height":
            CAMERA_HEIGHT,

        "person_roi":
            person_roi,

        "standing_roi":
            standing_roi,

        "right_vectors":
            right_vectors,

        "left_vectors":
            left_vectors
    }


    save_calibration(
        data
    )


    return data


# ============================================================
# OSC HELPERS
# ============================================================

def send_all_invalid(
    osc
):

    osc.send_message(
        "/arm/right/valid",
        0
    )


    osc.send_message(
        "/arm/left/valid",
        0
    )


def send_stop(
    osc
):

    osc.send_message(
        "/person/stop",
        1
    )


    send_all_invalid(
        osc
    )


# ============================================================
# MAIN
# ============================================================

def main():

    cv2.setUseOptimized(
        True
    )


    # ========================================================
    # OSC
    # ========================================================

    osc = SimpleUDPClient(
        OSC_IP,
        OSC_PORT
    )


    # ========================================================
    # CAMERA
    # ========================================================

    cap = cv2.VideoCapture(
        CAMERA_INDEX,
        cv2.CAP_DSHOW
    )


    cap.set(
        cv2.CAP_PROP_FRAME_WIDTH,
        CAMERA_WIDTH
    )


    cap.set(
        cv2.CAP_PROP_FRAME_HEIGHT,
        CAMERA_HEIGHT
    )


    success, _ = cap.read()


    if not success:

        raise RuntimeError(
            "Webcam error"
        )


    # ========================================================
    # MODEL
    # ========================================================

    print(
        "Loading RTMW3D..."
    )


    pose_model = RTMPose3d(

        onnx_model=
            RTMW3D_MODEL,

        model_input_size=
            MODEL_INPUT_SIZE,

        backend=
            "onnxruntime",

        device=
            "cpu",

        to_openpose=
            False
    )


    print(
        "RTMW3D ready"
    )


    # ========================================================
    # LOAD CALIBRATION
    # ========================================================

    calibration = load_calibration()


    if calibration is None:

        calibration = run_full_calibration(
            cap,
            pose_model
        )

    else:

        print(
            "Loaded:",
            CALIBRATION_FILE.resolve()
        )


    person_roi = calibration[
        "person_roi"
    ]


    standing_roi = calibration[
        "standing_roi"
    ]


    right_refs = np.asarray(
        calibration[
            "right_vectors"
        ],
        dtype=np.float32
    )


    left_refs = np.asarray(
        calibration[
            "left_vectors"
        ],
        dtype=np.float32
    )


    print()
    print(
        "q = quit"
    )
    print(
        "r = recalibrate everything"
    )
    print()


    # ========================================================
    # STATE
    # ========================================================

    active = False

    inside_since = None
    outside_since = None

    up_samples = []
    room_up = None

    frame_count = 0


    smoothed_azimuth = {
        "right": None,
        "left": None
    }


    smoothed_elevation = {
        "right": None,
        "left": None
    }


    last_values = {

        "right": {
            "azimuth": None,
            "elevation": None,
            "extension": 0.0,
            "valid": False,
            "map_error": None
        },

        "left": {
            "azimuth": None,
            "elevation": None,
            "extension": 0.0,
            "valid": False,
            "map_error": None
        }
    }


    last_pose = None

    person_inside = False

    waiting_elapsed = 0.0


    # ========================================================
    # LOOP
    # ========================================================

    try:

        while cap.isOpened():

            success, frame = cap.read()


            if not success:
                break


            frame_count += 1


            px, py, pw, ph = person_roi


            crop = frame[
                py:py + ph,
                px:px + pw
            ]


            # =================================================
            # INFERENCE
            # =================================================

            if (
                frame_count
                % PROCESS_EVERY_N_FRAMES
                == 0
            ):

                pose = infer_pose(
                    pose_model,
                    crop
                )


                if pose is not None:

                    last_pose = pose


                    k2d = pose[
                        "keypoints2d"
                    ]


                    scores = pose[
                        "scores"
                    ]


                    simcc = pose[
                        "simcc"
                    ]


                    person_inside = person_inside_standing_area(
                        k2d,
                        scores,
                        standing_roi
                    )


                    now = time.monotonic()


                    # =========================================
                    # WAITING
                    # =========================================

                    if not active:

                        outside_since = None


                        if person_inside:

                            if inside_since is None:

                                inside_since = now

                                up_samples = []


                            up_now = get_body_up(
                                simcc,
                                scores
                            )


                            if up_now is not None:

                                up_samples.append(
                                    up_now
                                )


                                if (
                                    len(up_samples)
                                    > 60
                                ):

                                    up_samples.pop(
                                        0
                                    )


                            waiting_elapsed = (
                                now
                                - inside_since
                            )


                            # =================================
                            # START
                            # =================================

                            if (
                                waiting_elapsed
                                >= ENTER_SECONDS
                            ):

                                if (
                                    len(up_samples)
                                    > 0
                                ):

                                    room_up = normalize(
                                        np.mean(
                                            np.asarray(
                                                up_samples
                                            ),
                                            axis=0
                                        )
                                    )

                                else:

                                    room_up = get_body_up(
                                        simcc,
                                        scores
                                    )


                                if room_up is not None:

                                    active = True


                                    smoothed_azimuth = {
                                        "right": None,
                                        "left": None
                                    }


                                    smoothed_elevation = {
                                        "right": None,
                                        "left": None
                                    }


                                    osc.send_message(
                                        "/person/start",
                                        1
                                    )


                                    print(
                                        ">>> START"
                                    )


                        else:

                            inside_since = None

                            up_samples = []

                            waiting_elapsed = 0.0


                    # =========================================
                    # ACTIVE / EXIT
                    # =========================================

                    else:

                        if person_inside:

                            outside_since = None


                        else:

                            # 所定位置外では、
                            # STOP猶予中も腕操作は無効
                            send_all_invalid(
                                osc
                            )


                            last_values[
                                "right"
                            ][
                                "valid"
                            ] = False


                            last_values[
                                "left"
                            ][
                                "valid"
                            ] = False


                            if outside_since is None:

                                outside_since = now


                            if (
                                now
                                - outside_since
                                >= EXIT_SECONDS
                            ):

                                active = False

                                inside_since = None

                                outside_since = None

                                room_up = None

                                up_samples = []

                                waiting_elapsed = 0.0


                                send_stop(
                                    osc
                                )


                                print(
                                    "<<< STOP"
                                )


                    # =========================================
                    # ARM CONTROL
                    # =========================================

                    if (
                        active
                        and
                        person_inside
                        and
                        room_up is not None
                    ):

                        for side, refs in (

                            (
                                "right",
                                right_refs
                            ),

                            (
                                "left",
                                left_refs
                            )

                        ):


                            arm2d = get_arm_2d(
                                k2d,
                                scores,
                                side
                            )


                            valid = False


                            if (
                                arm2d is not None
                                and
                                arm2d[
                                    "length_px"
                                ]
                                >= MIN_ARM_2D_PIXELS
                                and
                                arm2d[
                                    "extension"
                                ]
                                >= MIN_EXTENSION
                            ):

                                # =============================
                                # AZIMUTH = 2D CALIBRATION
                                # =============================

                                (
                                    raw_azimuth,
                                    map_error
                                ) = azimuth_from_calibration(

                                    arm2d[
                                        "direction"
                                    ],

                                    refs
                                )


                                # =============================
                                # ELEVATION = 3D
                                # =============================

                                raw_elevation = calculate_elevation(

                                    simcc,
                                    scores,
                                    side,
                                    room_up
                                )


                                # =============================
                                # VALID
                                # =============================

                                if (
                                    raw_azimuth
                                    is not None

                                    and
                                    raw_elevation
                                    is not None

                                    and
                                    map_error
                                    <= AZIMUTH_MAX_MAPPING_ERROR
                                ):

                                    azimuth = smooth_angle(

                                        smoothed_azimuth[
                                            side
                                        ],

                                        raw_azimuth,

                                        AZIMUTH_ALPHA
                                    )


                                    elevation = smooth_scalar(

                                        smoothed_elevation[
                                            side
                                        ],

                                        raw_elevation,

                                        ELEVATION_ALPHA
                                    )


                                    smoothed_azimuth[
                                        side
                                    ] = azimuth


                                    smoothed_elevation[
                                        side
                                    ] = elevation


                                    valid = True


                                    # =========================
                                    # OSC
                                    # =========================

                                    osc.send_message(

                                        f"/arm/{side}/azimuth",

                                        float(
                                            azimuth
                                        )
                                    )


                                    osc.send_message(

                                        f"/arm/{side}/elevation",

                                        float(
                                            elevation
                                        )
                                    )


                                    osc.send_message(

                                        f"/arm/{side}/extension",

                                        float(
                                            arm2d[
                                                "extension"
                                            ]
                                        )
                                    )


                                    osc.send_message(

                                        f"/arm/{side}/valid",

                                        1
                                    )


                                    last_values[
                                        side
                                    ] = {

                                        "azimuth":
                                            azimuth,

                                        "elevation":
                                            elevation,

                                        "extension":
                                            arm2d[
                                                "extension"
                                            ],

                                        "valid":
                                            True,

                                        "map_error":
                                            map_error
                                    }


                            # =============================
                            # INVALID
                            # =============================

                            if not valid:

                                osc.send_message(

                                    f"/arm/{side}/valid",

                                    0
                                )


                                last_values[
                                    side
                                ][
                                    "valid"
                                ] = False


            # =================================================
            # PREVIEW
            # =================================================

            if SHOW_PREVIEW:

                preview = crop.copy()


                draw_standing_area(
                    preview,
                    standing_roi
                )


                if last_pose is not None:

                    draw_body(

                        preview,

                        last_pose[
                            "keypoints2d"
                        ],

                        last_pose[
                            "scores"
                        ]
                    )


                # ------------------------------------------------
                # status
                # ------------------------------------------------

                if active:

                    status = "ACTIVE"

                    status_color = (
                        0,
                        255,
                        0
                    )


                elif person_inside:

                    remain = max(

                        0.0,

                        ENTER_SECONDS
                        - waiting_elapsed
                    )


                    status = (
                        f"START IN "
                        f"{remain:.1f}s"
                    )


                    status_color = (
                        0,
                        255,
                        255
                    )


                else:

                    status = "WAITING"

                    status_color = (
                        0,
                        0,
                        255
                    )


                cv2.putText(

                    preview,

                    status,

                    (15, 28),

                    cv2.FONT_HERSHEY_SIMPLEX,

                    0.65,

                    status_color,

                    2
                )


                # ------------------------------------------------
                # arm values
                # ------------------------------------------------

                y = 55


                for side in (
                    "right",
                    "left"
                ):

                    value = last_values[
                        side
                    ]


                    if (
                        value[
                            "azimuth"
                        ]
                        is None
                    ):

                        text = (
                            f"{side}: ---"
                        )


                    else:

                        text = (

                            f"{side}: "

                            f"az="
                            f"{value['azimuth']:6.1f} "

                            f"el="
                            f"{value['elevation']:6.1f} "

                            f"ext="
                            f"{value['extension']:.2f} "

                            f"{'ON' if value['valid'] else 'OFF'}"
                        )


                    cv2.putText(

                        preview,

                        text,

                        (15, y),

                        cv2.FONT_HERSHEY_SIMPLEX,

                        0.48,

                        (255, 255, 255),

                        1
                    )


                    y += 24


                cv2.putText(

                    preview,

                    "Q quit   R recalibrate",

                    (
                        15,
                        preview.shape[0]
                        - 15
                    ),

                    cv2.FONT_HERSHEY_SIMPLEX,

                    0.45,

                    (255, 255, 255),

                    1
                )


                cv2.imshow(

                    "Hybrid Pose -> OSC",

                    preview
                )


            # =================================================
            # KEYBOARD
            # =================================================

            key = (
                cv2.waitKey(1)
                & 0xFF
            )


            # Q
            if key == ord("q"):
                break


            # =================================================
            # R = RECALIBRATE
            # =================================================

            if key == ord("r"):

                if active:

                    send_stop(
                        osc
                    )


                active = False

                inside_since = None
                outside_since = None

                room_up = None

                up_samples = []


                cv2.destroyWindow(
                    "Hybrid Pose -> OSC"
                )


                calibration = run_full_calibration(
                    cap,
                    pose_model
                )


                person_roi = calibration[
                    "person_roi"
                ]


                standing_roi = calibration[
                    "standing_roi"
                ]


                right_refs = np.asarray(

                    calibration[
                        "right_vectors"
                    ],

                    dtype=np.float32
                )


                left_refs = np.asarray(

                    calibration[
                        "left_vectors"
                    ],

                    dtype=np.float32
                )


                last_pose = None


                smoothed_azimuth = {
                    "right": None,
                    "left": None
                }


                smoothed_elevation = {
                    "right": None,
                    "left": None
                }


    # ========================================================
    # CLOSE
    # ========================================================

    finally:

        if active:

            send_stop(
                osc
            )

        else:

            send_all_invalid(
                osc
            )


        cap.release()

        cv2.destroyAllWindows()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()