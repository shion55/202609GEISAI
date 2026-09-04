import cv2
import json
import time
import threading
import ctypes
import sys
from collections import deque
from pathlib import Path

import numpy as np
from rtmlib import RTMPose3d
from pythonosc.udp_client import SimpleUDPClient
from pythonosc.osc_bundle_builder import OscBundleBuilder, IMMEDIATELY
from pythonosc.osc_message_builder import OscMessageBuilder



# ============================================================
# RECOVERY MODE
# ============================================================
# 安定しているv13の腕推定はそのまま。
# 左右raw OSCは変更せず、OSC送信層だけで共通 /arm/* を選択する版。

# ============================================================
# SETTINGS
# ============================================================

CAMERA_INDEX = 0
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
CAMERA_FPS = 30.0
CAMERA_USE_MJPG = True

OSC_IP = "127.0.0.1"
OSC_PORT = 8000

# OSC送信レート。推定は毎フレーム行うが、Maxへの送信はこのHzに制限する。
# 30Hzで十分滑らか。重ければ20、より細かくしたければ40程度。
OSC_SEND_HZ = 30.0
OSC_SEND_INTERVAL = 1.0 / OSC_SEND_HZ

WINDOWS_ABOVE_NORMAL_PRIORITY = True
WINDOWS_FORCE_HIGH_QOS = True

# 人が所定位置に入ってから開始するまで
ENTER_SECONDS = 2.0

# 一瞬見失っただけでは止めない
EXIT_SECONDS = 0.7

# Pose confidence
JOINT_THRESHOLD = 0.35
PERSON_MEAN_SCORE_THRESHOLD = 0.40
PERSON_MIN_VISIBLE_JOINTS = 8

# 立ち位置判定は床との接地点に近い足首を最優先する。
# 足首は腰/肩よりconfidenceが下がりやすいため、少しだけ閾値を低くする。
FOOT_POSITION_JOINT_THRESHOLD = 0.20
POSITION_JOINT_THRESHOLD = 0.25

# 腕方向は肩と手首を必須、肘は任意。
ARM_REQUIRED_JOINT_THRESHOLD = 0.30
ELBOW_OPTIONAL_THRESHOLD = 0.25

# 腕の有効判定
MIN_EXTENSION = 0.72
MIN_ARM_2D_PIXELS = 15.0

# ============================================================
# ACTIVE ARM SELECTION
# ============================================================

# 『腕を出しているか』は、肘の伸び具合(extension)ではなく、
# 3Dで身体の縦軸からどれだけ横へ腕が離れているかで判定する。
# これならカメラ軸方向で2Dの肩→手首距離が短くなっても選択が切れにくい。
ACTIVE_ARM_ENTER_STRENGTH = 0.60
ACTIVE_ARM_EXIT_STRENGTH = 0.42


# ============================================================
# SPATIAL LOCK (NO YOLOX)
# ============================================================

# Standing Circleを中心に、この倍率までをPose推定に見せる。
# 周囲の人物はマスクしてRTMPose3dから除外する。
# 腕がマスク外へ切れない範囲で、小さいほど周囲の人に強い。
SPATIAL_LOCK_RADIUS_SCALE = 4
# マスク外を真っ黒にする。Falseならマスク処理をしない。
SPATIAL_LOCK_ENABLED = True

# Runtime only: RTMPose3d が複数人を返したとき、
# Standing Circle 中心に近い人物だけを採用する。
# キャリブレーションには使わない。
RUNTIME_PERSON_LOCK_RADIUS_SCALE = 1.35

# ============================================================
# BODY ORIENTATION
# ============================================================

# body azimuthは左右肩の軸を基本にし、鼻の位置で前後を決める。
BODY_JOINT_THRESHOLD = 0.25
BODY_ALPHA_SLOW = 0.35
BODY_ALPHA_FAST = 0.85
BODY_FAST_THRESHOLD = 10.0

# ============================================================
# CALIBRATION
# ============================================================

# 16方向 = 22.5度刻み
CALIBRATION_DIRECTIONS = 16
CALIBRATION_SAMPLES = 8
CALIBRATION_MIN_EXTENSION = 0.72

# Calibration quality gate. Camera-axis directions can look very short in 2D,
# so calibration must NOT require a minimum 2D shoulder->wrist pixel length.
# Instead, require a confident shoulder/wrist and a plausible 3D arm length.
CALIBRATION_ARM_CONFIDENCE = 0.30
CALIBRATION_MIN_ARM_3D_TORSO = 0.45
CALIBRATION_MAX_ARM_3D_TORSO = 2.60
CALIBRATION_READY_CONSECUTIVE_FRAMES = 5
CALIBRATION_RECORD_INVALID_RESET_FRAMES = 3
CALIBRATION_DIRECTION_REMINDER_SECONDS = 1.0

# PCから立ち位置へ移動する時間
CALIBRATION_START_DELAY = 10.0

# 各方向を向く準備時間
# この3秒間、1秒ごとに3回Maxへ方向OSCを送る
CALIBRATION_PREPARE_SECONDS = 1.0

# 8フレーム取得後、次方向まで無音で待つ
CALIBRATION_GAP_SECONDS = 1.0

# Standing Circleを外れたときの猶予
# 厳密に即停止なら 0.0
CALIBRATION_EXIT_GRACE_SECONDS = 0.75

# Calibration never aborts automatically on an outside judgment.
# Use a slightly wider circle only for calibration to absorb pose-center jitter.
CALIBRATION_INSIDE_RADIUS_SCALE = 1.35

# キャリブレーション画面だけ大きく表示
CALIBRATION_DISPLAY_SCALE = 3.0

# 旧版と混ざらないよう別ファイル
CALIBRATION_FILE = Path("calibration_onecam_spatiallock_720p_v9_axis_safe_pause.json")
CALIBRATION_VERSION = 9

# ============================================================
# AZIMUTH ESTIMATOR
# ============================================================

# 2D特徴 = (wrist - shoulder) / torso_length
# この軌道への距離が大きすぎる場合は無効
MAX_FEATURE_ERROR = 0.65

# 通常領域：2D観測主体。
# azimuthについてRTMW3Dのdepthは使わない。
# 単眼depthの前後反転が180°ジャンプの主因になるため。
CONTINUITY_WEIGHT_NORMAL = 0.18

# カメラ軸領域はヒステリシスで判定する。
# feature length = |shoulder->wrist| / torso_length
# ENTERより短くなったらAXISに入り、EXITより長くなるまで抜けない。
AXIS_ENTER_LENGTH = 0.58
AXIS_EXIT_LENGTH = 0.72

# AXIS中は観測角を完全に無視し、直前の角度＋角速度で橋渡しする。
# 角速度は毎フレーム減衰させ、軸上で腕を止めても角度が漂い続けないようにする。
AXIS_VELOCITY_DECAY = 0.82
AXIS_MIN_VELOCITY = 6.0  # deg/sec 未満は0として固定

# 軸を抜けた直後を含め、予測角からこれ以上離れた候補は採用しない。
# 180°反対側への再ロックを防ぐためのハードゲート。
MAX_REACQUIRE_DISTANCE = 70.0  # deg

# depthはデバッグ・将来用途のため履歴だけ保持するが、azimuth選択には使わない。
DEPTH_HISTORY = 9

# azimuth adaptive smoothing
# 小さな揺れは抑えつつ、大きく動いたときはすぐ追従する。
AZIMUTH_ALPHA_SLOW = 0.40
AZIMUTH_ALPHA_FAST = 0.90
AZIMUTH_FAST_THRESHOLD = 12.0  # deg

# 角速度 smoothing（AXIS通過時の予測用）
VELOCITY_ALPHA = 0.55
MAX_ANGULAR_VELOCITY = 420.0  # deg/sec

# 腕が無効になってからこの秒数までは以前の角度状態を保持
STATE_HOLD_SECONDS = 2.0

# ============================================================
# ELEVATION
# ============================================================

# elevationも固定alphaではなく可変にする。
ELEVATION_ALPHA_SLOW = 0.30
ELEVATION_ALPHA_FAST = 0.85
ELEVATION_FAST_THRESHOLD = 8.0  # deg
ELEVATION_SIGN = 1.0
ELEVATION_OFFSET = 0.0

# ============================================================
# PERFORMANCE / PREVIEW
# ============================================================

PROCESS_EVERY_N_FRAMES = 1
SHOW_PREVIEW = True

# ============================================================
# RTMW3D
# ============================================================

RTMW3D_MODEL = (
    "https://huggingface.co/Soykaf/RTMW3D-x/resolve/main/onnx/"
    "rtmw3d-x_8xb64_cocktail14-384x288-b0a0eab7_20240626.onnx"
)
MODEL_INPUT_SIZE = (288, 384)

# ============================================================
# COCO BODY INDICES
# ============================================================

NOSE = 0
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
    LEFT_SHOULDER, RIGHT_SHOULDER,
    LEFT_ELBOW, RIGHT_ELBOW,
    LEFT_WRIST, RIGHT_WRIST,
    LEFT_HIP, RIGHT_HIP,
    LEFT_KNEE, RIGHT_KNEE,
    LEFT_ANKLE, RIGHT_ANKLE,
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
# WINDOWS SCHEDULING
# ============================================================

def set_windows_above_normal_priority():
    if not WINDOWS_ABOVE_NORMAL_PRIORITY or sys.platform != "win32":
        return
    try:
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.kernel32.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS)
        if ok:
            print("Windows process priority: ABOVE_NORMAL")
    except Exception as e:
        print("Priority setting skipped:", e)

def set_windows_high_qos():
    """
    Windows 11 の visibility-based QoS / EcoQoS による
    バックグラウンド時の実行速度低下を明示的に無効化する。

    PROCESS_POWER_THROTTLING_EXECUTION_SPEED を ControlMask に指定し、
    StateMask=0 にすることで HighQoS を要求する。
    """
    if not WINDOWS_FORCE_HIGH_QOS or sys.platform != "win32":
        return

    try:
        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        # Windows SDK constants
        ProcessPowerThrottling = 4
        PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1

        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        state.StateMask = 0  # Execution-speed throttling OFF = HighQoS

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetProcessInformation.restype = ctypes.c_bool

        handle = kernel32.GetCurrentProcess()
        ok = kernel32.SetProcessInformation(
            handle,
            ProcessPowerThrottling,
            ctypes.byref(state),
            ctypes.sizeof(state),
        )

        if ok:
            print("Windows QoS: HIGH (execution-speed throttling disabled)")
        else:
            err = ctypes.get_last_error()
            print(f"Windows HighQoS request failed: error {err}")

    except Exception as e:
        print("HighQoS setting skipped:", e)


# ============================================================
# BASIC UTILS
# ============================================================

def normalize(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    if n < 1e-8:
        return None
    return v / n


def signed_angle_delta(target, source):
    """source -> target の最短符号付き角度差 (-180..180]"""
    return ((target - source + 180.0) % 360.0) - 180.0


def angular_distance(a, b):
    return abs(signed_angle_delta(a, b))


def smooth_angle(previous, current, alpha):
    if previous is None:
        return float(current % 360.0)
    return float((previous + alpha * signed_angle_delta(current, previous)) % 360.0)


def smooth_scalar(previous, current, alpha):
    if previous is None:
        return float(current)
    return float((1.0 - alpha) * previous + alpha * current)


def smooth_angle_adaptive(previous, current):
    """
    角度差が大きいほどalphaを上げて低遅延にする。
    小さい揺れだけはある程度平滑化する。
    """
    if previous is None:
        return float(current % 360.0)

    diff = abs(signed_angle_delta(current, previous))
    ratio = float(np.clip(diff / AZIMUTH_FAST_THRESHOLD, 0.0, 1.0))
    alpha = (
        AZIMUTH_ALPHA_SLOW
        + (AZIMUTH_ALPHA_FAST - AZIMUTH_ALPHA_SLOW) * ratio
    )
    return smooth_angle(previous, current, alpha)


def smooth_scalar_adaptive(previous, current):
    """
    elevation用。変化が大きいときは高速追従、静止時だけ滑らかにする。
    """
    if previous is None:
        return float(current)

    diff = abs(float(current) - float(previous))
    ratio = float(np.clip(diff / ELEVATION_FAST_THRESHOLD, 0.0, 1.0))
    alpha = (
        ELEVATION_ALPHA_SLOW
        + (ELEVATION_ALPHA_FAST - ELEVATION_ALPHA_SLOW) * ratio
    )
    return smooth_scalar(previous, current, alpha)


def arm_indices(side):
    if side == "right":
        return RIGHT_SHOULDER, RIGHT_ELBOW, RIGHT_WRIST
    return LEFT_SHOULDER, LEFT_ELBOW, LEFT_WRIST


def torso_length_2d(kpts2d, scores):
    """
    2Dの身体スケール。
    真上寄りでは4点全部が同時に見えないことがあるため、
    左右それぞれの肩-腰距離のうち取得できたものを平均する。
    """
    lengths = []

    for shoulder_idx, hip_idx in (
        (LEFT_SHOULDER, LEFT_HIP),
        (RIGHT_SHOULDER, RIGHT_HIP),
    ):
        if (
            scores[shoulder_idx] >= POSITION_JOINT_THRESHOLD
            and scores[hip_idx] >= POSITION_JOINT_THRESHOLD
        ):
            shoulder = np.asarray(kpts2d[shoulder_idx], dtype=np.float32)
            hip = np.asarray(kpts2d[hip_idx], dtype=np.float32)
            length = float(np.linalg.norm(shoulder - hip))
            if length > 1e-6:
                lengths.append(length)

    if not lengths:
        return None

    return float(np.mean(lengths))


def arm_extension_2d(shoulder, elbow, wrist):
    straight = np.linalg.norm(wrist - shoulder)
    upper = np.linalg.norm(elbow - shoulder)
    lower = np.linalg.norm(wrist - elbow)
    total = upper + lower
    if total < 1e-8:
        return 0.0
    return float(np.clip(straight / total, 0.0, 1.0))


# ============================================================
# 2D + DEPTH ARM OBSERVATION
# ============================================================

def simcc_to_normalized_xyz(kpts_simcc):
    pts = np.asarray(kpts_simcc, dtype=np.float32).copy()
    pts[:, 0] /= float(MODEL_INPUT_SIZE[0])
    pts[:, 1] /= float(MODEL_INPUT_SIZE[1])
    pts[:, 2] /= float(MODEL_INPUT_SIZE[1])
    return pts


def torso_length_3d(kpts_simcc, scores):
    """3Dの身体スケール。左右どちらかの肩-腰が取れれば使用する。"""
    pts = simcc_to_normalized_xyz(kpts_simcc)
    lengths = []

    for shoulder_idx, hip_idx in (
        (LEFT_SHOULDER, LEFT_HIP),
        (RIGHT_SHOULDER, RIGHT_HIP),
    ):
        if (
            scores[shoulder_idx] >= POSITION_JOINT_THRESHOLD
            and scores[hip_idx] >= POSITION_JOINT_THRESHOLD
        ):
            length = float(np.linalg.norm(pts[shoulder_idx] - pts[hip_idx]))
            if length > 1e-6:
                lengths.append(length)

    if not lengths:
        return None

    return float(np.mean(lengths))


def get_arm_observation(kpts2d, kpts_simcc, scores, side):
    """
    真上寄りカメラ向けの腕観測。

    - 肩と手首: 必須
    - 肘: 任意。見失った場合も方向追跡は継続する
    """
    shoulder_idx, elbow_idx, wrist_idx = arm_indices(side)

    shoulder_conf = float(scores[shoulder_idx])
    elbow_conf = float(scores[elbow_idx])
    wrist_conf = float(scores[wrist_idx])

    if (
        shoulder_conf < ARM_REQUIRED_JOINT_THRESHOLD
        or wrist_conf < ARM_REQUIRED_JOINT_THRESHOLD
    ):
        return None

    shoulder = np.asarray(kpts2d[shoulder_idx], dtype=np.float32)
    wrist = np.asarray(kpts2d[wrist_idx], dtype=np.float32)

    torso2d = torso_length_2d(kpts2d, scores)
    torso3d = torso_length_3d(kpts_simcc, scores)
    if torso2d is None or torso3d is None:
        return None

    vec2d = wrist - shoulder
    length_px = float(np.linalg.norm(vec2d))
    feature2d = vec2d / torso2d
    feature_length = float(np.linalg.norm(feature2d))

    # 肘が取れるときだけ「まっすぐ伸びているか」を計算する。
    # 肘だけ隠れた場合は、肩→手首方向を捨てない。
    if elbow_conf >= ELBOW_OPTIONAL_THRESHOLD:
        elbow = np.asarray(kpts2d[elbow_idx], dtype=np.float32)
        extension = arm_extension_2d(shoulder, elbow, wrist)
    else:
        extension = 1.0

    pts3d = simcc_to_normalized_xyz(kpts_simcc)
    depth = float((pts3d[wrist_idx][2] - pts3d[shoulder_idx][2]) / torso3d)

    return {
        "feature2d": feature2d.astype(np.float32),
        "feature_length": feature_length,
        "length_px": length_px,
        "depth": depth,
        "extension": extension,
        "confidence": min(shoulder_conf, wrist_conf),
        "elbow_visible": elbow_conf >= ELBOW_OPTIONAL_THRESHOLD,
    }


def arm_out_strength_3d(kpts_simcc, scores, side, room_up):
    """
    身体の縦軸に対して、肩→手首がどれだけ横へ張り出しているか。

    - 腕を身体の横へ出す: 大きい
    - 腕を身体の横に下ろす: 小さい
    - カメラ軸方向を指す: 2Dでは短く見えても3Dでは大きいまま

    torso長で正規化するので、画面上の人物サイズに依存しにくい。
    """
    if room_up is None:
        return 0.0

    shoulder_idx, _, wrist_idx = arm_indices(side)
    if (
        scores[shoulder_idx] < ARM_REQUIRED_JOINT_THRESHOLD
        or scores[wrist_idx] < ARM_REQUIRED_JOINT_THRESHOLD
    ):
        return 0.0

    torso3d = torso_length_3d(kpts_simcc, scores)
    if torso3d is None or torso3d < 1e-8:
        return 0.0

    pts = simcc_to_normalized_xyz(kpts_simcc)
    arm = pts[wrist_idx] - pts[shoulder_idx]
    vertical = float(np.dot(arm, room_up))
    horizontal_vec = arm - vertical * room_up
    horizontal = float(np.linalg.norm(horizontal_vec))
    return horizontal / float(torso3d)


class ActiveArmSelector:
    """
    右だけ出す→right、左だけ出す→left、両腕→片方だけ。

    両腕が出ているときは現在選択中の腕を維持する。
    未選択から両腕が同時に出た場合だけ、より大きく出ている側を選ぶ。
    ENTER/EXITのヒステリシスで境界付近の左右チラつきを防ぐ。
    """

    def __init__(self):
        self.selected = None

    def reset(self):
        self.selected = None

    def update(self, strengths):
        right = float(strengths.get("right", 0.0))
        left = float(strengths.get("left", 0.0))

        right_enter = right >= ACTIVE_ARM_ENTER_STRENGTH
        left_enter = left >= ACTIVE_ARM_ENTER_STRENGTH
        right_keep = right >= ACTIVE_ARM_EXIT_STRENGTH
        left_keep = left >= ACTIVE_ARM_EXIT_STRENGTH

        # 選択中の腕がまだ十分出ているなら、その腕を優先して維持。
        # ただし選択腕が引っ込み、反対腕が明確に出たら即切り替える。
        if self.selected == "right":
            if right_keep:
                return "right"
            if left_enter:
                self.selected = "left"
                return "left"
            self.selected = None
            return None

        if self.selected == "left":
            if left_keep:
                return "left"
            if right_enter:
                self.selected = "right"
                return "right"
            self.selected = None
            return None

        # 未選択時
        if right_enter and not left_enter:
            self.selected = "right"
        elif left_enter and not right_enter:
            self.selected = "left"
        elif right_enter and left_enter:
            self.selected = "right" if right >= left else "left"
        else:
            self.selected = None

        return self.selected


def build_selected_arm_output(values, selected_side):
    """選択された片腕だけvalidを残す。"""
    output = {
        "right": dict(values["right"]),
        "left": dict(values["left"]),
    }
    for side in ("right", "left"):
        if side != selected_side:
            output[side]["valid"] = False
    return output


# ============================================================
# ELEVATION
# ============================================================

def get_body_up(kpts_simcc, scores):
    """左右どちらかの肩-腰が取れれば身体の上方向を推定する。"""
    pts = simcc_to_normalized_xyz(kpts_simcc)
    up_vectors = []

    for shoulder_idx, hip_idx in (
        (LEFT_SHOULDER, LEFT_HIP),
        (RIGHT_SHOULDER, RIGHT_HIP),
    ):
        if (
            scores[shoulder_idx] >= POSITION_JOINT_THRESHOLD
            and scores[hip_idx] >= POSITION_JOINT_THRESHOLD
        ):
            up = normalize(pts[shoulder_idx] - pts[hip_idx])
            if up is not None:
                up_vectors.append(up)

    if not up_vectors:
        return None

    return normalize(np.mean(np.asarray(up_vectors), axis=0))


def calculate_elevation(kpts_simcc, scores, side, room_up):
    if room_up is None:
        return None

    shoulder_idx, _, wrist_idx = arm_indices(side)
    if min(scores[shoulder_idx], scores[wrist_idx]) < JOINT_THRESHOLD:
        return None

    pts = simcc_to_normalized_xyz(kpts_simcc)
    arm = pts[wrist_idx] - pts[shoulder_idx]
    if np.linalg.norm(arm) < 1e-8:
        return None

    vertical = float(np.dot(arm, room_up))
    horizontal_vec = arm - vertical * room_up
    horizontal = float(np.linalg.norm(horizontal_vec))

    elevation = np.degrees(np.arctan2(vertical, horizontal))
    elevation = ELEVATION_SIGN * elevation + ELEVATION_OFFSET
    return float(np.clip(elevation, -90.0, 90.0))


# ============================================================
# PERSON POSITION
# ============================================================

def get_person_center_2d(kpts2d, scores):
    """
    Standing Circle用の位置点。床上の立ち位置に合わせて足首を最優先する。

    優先順位:
    1) 左右足首が見える   -> 左右足首の中点
    2) 片足首だけ見える   -> その足首
    3) 足首が取れない     -> 左右腰の中点
    4) 腰も取れない       -> 左右肩の中点
    5) 最後のfallback      -> 見えている腰/肩の平均

    方向キャリブレーションの feature2d / depth 計算には使わない。
    入場・退出・人物選択・キャリブレーション中の立ち位置判定だけに使う。
    """
    ankles = []
    for idx in (LEFT_ANKLE, RIGHT_ANKLE):
        if scores[idx] >= FOOT_POSITION_JOINT_THRESHOLD:
            ankles.append(np.asarray(kpts2d[idx], dtype=np.float32))

    if len(ankles) == 2:
        return (ankles[0] + ankles[1]) / 2.0
    if len(ankles) == 1:
        return ankles[0].copy()

    if (
        scores[LEFT_HIP] >= POSITION_JOINT_THRESHOLD
        and scores[RIGHT_HIP] >= POSITION_JOINT_THRESHOLD
    ):
        return (
            np.asarray(kpts2d[LEFT_HIP], dtype=np.float32)
            + np.asarray(kpts2d[RIGHT_HIP], dtype=np.float32)
        ) / 2.0

    if (
        scores[LEFT_SHOULDER] >= POSITION_JOINT_THRESHOLD
        and scores[RIGHT_SHOULDER] >= POSITION_JOINT_THRESHOLD
    ):
        return (
            np.asarray(kpts2d[LEFT_SHOULDER], dtype=np.float32)
            + np.asarray(kpts2d[RIGHT_SHOULDER], dtype=np.float32)
        ) / 2.0

    visible = []
    for idx in (LEFT_HIP, RIGHT_HIP, LEFT_SHOULDER, RIGHT_SHOULDER):
        if scores[idx] >= POSITION_JOINT_THRESHOLD:
            visible.append(np.asarray(kpts2d[idx], dtype=np.float32))

    if len(visible) >= 2:
        return np.mean(np.asarray(visible), axis=0)

    return None


def person_inside_standing_area(kpts2d, scores, standing_circle):
    cx, cy, radius = standing_circle

    center = get_person_center_2d(kpts2d, scores)
    if center is None:
        return False

    dx = float(center[0]) - float(cx)
    dy = float(center[1]) - float(cy)

    return (dx * dx + dy * dy) <= float(radius * radius)


def person_inside_calibration_area(kpts2d, scores, standing_circle):
    """Calibration-only standing-area test with a small jitter margin."""
    cx, cy, radius = standing_circle
    center = get_person_center_2d(kpts2d, scores)
    if center is None:
        return False
    dx = float(center[0]) - float(cx)
    dy = float(center[1]) - float(cy)
    limit = float(radius) * CALIBRATION_INSIDE_RADIUS_SCALE
    return (dx * dx + dy * dy) <= limit * limit


def apply_spatial_lock_mask(image, standing_circle):
    """
    Standing Circle周辺だけをRTMPose3dに見せる。
    画像サイズ・座標系は変えないので、キャリブレーション座標はそのまま使える。
    """
    if (
        not SPATIAL_LOCK_ENABLED
        or standing_circle is None
    ):
        return image

    cx, cy, radius = standing_circle
    h, w = image.shape[:2]

    mask_radius = int(max(1.0, float(radius) * SPATIAL_LOCK_RADIUS_SCALE))
    center = (int(round(cx)), int(round(cy)))

    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, center, mask_radius, 255, -1)

    return cv2.bitwise_and(image, image, mask=mask)


def build_direction_refs(right_refs, left_refs):
    """
    腕キャリブレーションから、画像上の方向ベクトル→物理azimuthの対応を作る。
    body orientationでも同じカメラ座標→実空間角度変換として利用する。
    """
    refs = []
    n = min(len(right_refs), len(left_refs))

    for i in range(n):
        vr = normalize(np.asarray(right_refs[i]["feature2d"], dtype=np.float32))
        vl = normalize(np.asarray(left_refs[i]["feature2d"], dtype=np.float32))

        vectors = [v for v in (vr, vl) if v is not None]
        if not vectors:
            continue

        mean = normalize(np.mean(np.asarray(vectors), axis=0))
        if mean is None:
            continue

        refs.append({
            "angle": float(right_refs[i]["angle"]),
            "direction": mean,
        })

    return refs


def map_image_direction_to_world(direction, refs):
    current = normalize(direction)
    if current is None or len(refs) < 3:
        return None

    best = None
    n = len(refs)
    step = 360.0 / n

    for i in range(n):
        a = np.asarray(refs[i]["direction"], dtype=np.float32)
        b = np.asarray(refs[(i + 1) % n]["direction"], dtype=np.float32)
        seg = b - a
        denom = float(np.dot(seg, seg))

        if denom < 1e-8:
            t = 0.0
            projected = a
        else:
            t = float(np.clip(np.dot(current - a, seg) / denom, 0.0, 1.0))
            projected = a + t * seg

        error = float(np.linalg.norm(current - projected))
        angle = (float(refs[i]["angle"]) + t * step) % 360.0

        if best is None or error < best[0]:
            best = (error, angle)

    return None if best is None else float(best[1])


def get_body_facing_direction_2d(kpts2d, scores, previous_angle, direction_refs):
    """
    左右肩を結ぶ線の垂直方向を身体の前方向候補とする。
    鼻が見えるときは鼻側を前として180°曖昧性を解消。
    鼻が弱いときは前回body azimuthに近い側を採用。
    """
    if (
        scores[LEFT_SHOULDER] < BODY_JOINT_THRESHOLD
        or scores[RIGHT_SHOULDER] < BODY_JOINT_THRESHOLD
    ):
        return None

    left = np.asarray(kpts2d[LEFT_SHOULDER], dtype=np.float32)
    right = np.asarray(kpts2d[RIGHT_SHOULDER], dtype=np.float32)
    center = (left + right) * 0.5
    shoulder_axis = normalize(right - left)
    if shoulder_axis is None:
        return None

    candidate_a = np.array([-shoulder_axis[1], shoulder_axis[0]], dtype=np.float32)
    candidate_b = -candidate_a

    if scores[NOSE] >= BODY_JOINT_THRESHOLD:
        nose = np.asarray(kpts2d[NOSE], dtype=np.float32)
        head_dir = normalize(nose - center)
        if head_dir is not None:
            return candidate_a if np.dot(candidate_a, head_dir) >= np.dot(candidate_b, head_dir) else candidate_b

    if previous_angle is not None:
        a_world = map_image_direction_to_world(candidate_a, direction_refs)
        b_world = map_image_direction_to_world(candidate_b, direction_refs)
        if a_world is not None and b_world is not None:
            return candidate_a if angular_distance(a_world, previous_angle) <= angular_distance(b_world, previous_angle) else candidate_b

    return None


def smooth_body_angle(previous, current):
    if previous is None:
        return float(current % 360.0)

    diff = abs(signed_angle_delta(current, previous))
    t = min(1.0, diff / max(1e-6, BODY_FAST_THRESHOLD))
    alpha = BODY_ALPHA_SLOW + (BODY_ALPHA_FAST - BODY_ALPHA_SLOW) * t
    return smooth_angle(previous, current, alpha)


# ============================================================
# AZIMUTH STATE ESTIMATOR
# ============================================================

class AzimuthState:
    def __init__(self):
        self.angle = None
        self.velocity = 0.0
        self.last_time = None
        self.invalid_since = None
        self.depth_history = deque(maxlen=DEPTH_HISTORY)
        self.in_axis_zone = False

    def reset(self):
        self.angle = None
        self.velocity = 0.0
        self.last_time = None
        self.invalid_since = None
        self.depth_history.clear()
        self.in_axis_zone = False

    def mark_invalid(self, now):
        if self.invalid_since is None:
            self.invalid_since = now
        if now - self.invalid_since > STATE_HOLD_SECONDS:
            self.reset()

    def _update_axis_zone(self, feature_len):
        # ヒステリシス：境界付近でAXIS/通常が毎フレーム切り替わらないようにする。
        if self.in_axis_zone:
            if feature_len > AXIS_EXIT_LENGTH:
                self.in_axis_zone = False
        else:
            if feature_len < AXIS_ENTER_LENGTH:
                self.in_axis_zone = True
        return self.in_axis_zone

    def update(self, current_feature, current_depth, refs, now):
        """
        refs: list of dict {angle, feature2d[2], depth}
        return angle, feature_error, is_axis_zone

        方針：
        - 通常領域：2Dキャリブレーション軌道 + 時間連続性
        - カメラ軸領域：現在フレームのazimuth観測を完全に無視
                         直前状態だけで橋渡し
        - depthはazimuth選択に使用しない
        """
        self.invalid_since = None
        self.depth_history.append(float(current_depth))

        if self.last_time is None:
            dt = 1.0 / 30.0
        else:
            dt = max(1e-3, min(0.2, now - self.last_time))

        predicted = None
        if self.angle is not None:
            predicted = (self.angle + self.velocity * dt) % 360.0

        feature_len = float(np.linalg.norm(current_feature))
        axis_zone = self._update_axis_zone(feature_len)

        # ====================================================
        # CAMERA AXIS LOCK
        # ====================================================
        # ここでは2Dもdepthも「どちら側か」の判定に使わない。
        # 単眼で前後が曖昧になる領域なので、観測を捨てて状態だけで通過する。
        if axis_zone and self.angle is not None:
            if predicted is not None:
                self.angle = predicted

            self.velocity *= AXIS_VELOCITY_DECAY
            if abs(self.velocity) < AXIS_MIN_VELOCITY:
                self.velocity = 0.0

            self.last_time = now
            return self.angle, 0.0, True

        n = len(refs)
        if n < 3:
            return None, 999.0, axis_zone

        best = None
        step = 360.0 / n

        for i in range(n):
            ra = refs[i]
            rb = refs[(i + 1) % n]

            a = np.asarray(ra["feature2d"], dtype=np.float32)
            b = np.asarray(rb["feature2d"], dtype=np.float32)
            seg = b - a
            denom = float(np.dot(seg, seg))

            if denom < 1e-8:
                t = 0.0
                projected = a
            else:
                t = float(np.clip(
                    np.dot(current_feature - a, seg) / denom,
                    0.0,
                    1.0
                ))
                projected = a + t * seg

            feature_error = float(np.linalg.norm(current_feature - projected))
            angle = (float(ra["angle"]) + t * step) % 360.0

            continuity_error_deg = 0.0
            if predicted is not None:
                continuity_error_deg = angular_distance(angle, predicted)

                # 物理的に1フレームで反対側へ飛ぶことはない。
                # 予測から遠すぎる候補は最初から候補外にする。
                if continuity_error_deg > MAX_REACQUIRE_DISTANCE:
                    continue

            continuity_error = continuity_error_deg / 180.0
            cost = (
                feature_error
                + CONTINUITY_WEIGHT_NORMAL * continuity_error
            )

            if best is None or cost < best[0]:
                best = (cost, angle, feature_error)

        # 全候補がハードゲートで消えた場合は、反対側へ飛ばず予測を保持する。
        if best is None:
            if predicted is not None:
                self.angle = predicted
                self.velocity *= 0.90
                if abs(self.velocity) < AXIS_MIN_VELOCITY:
                    self.velocity = 0.0
                self.last_time = now
                return self.angle, 999.0, axis_zone
            return None, 999.0, axis_zone

        _, raw_angle, feature_error = best

        # 軌道から離れすぎた観測も、状態があるなら反対側へ再ロックせず保持する。
        if feature_error > MAX_FEATURE_ERROR:
            if predicted is not None:
                self.angle = predicted
                self.velocity *= 0.90
                if abs(self.velocity) < AXIS_MIN_VELOCITY:
                    self.velocity = 0.0
                self.last_time = now
                return self.angle, feature_error, axis_zone
            return None, feature_error, axis_zone

        new_angle = smooth_angle_adaptive(self.angle, raw_angle)

        if self.angle is not None:
            delta = signed_angle_delta(new_angle, self.angle)
            instant_velocity = float(np.clip(
                delta / dt,
                -MAX_ANGULAR_VELOCITY,
                MAX_ANGULAR_VELOCITY
            ))
            self.velocity = (
                (1.0 - VELOCITY_ALPHA) * self.velocity
                + VELOCITY_ALPHA * instant_velocity
            )

        self.angle = new_angle
        self.last_time = now
        return self.angle, feature_error, axis_zone


# ============================================================
# DRAW / PREVIEW
# ============================================================

def draw_body(image, kpts2d, scores):
    for a, b in BODY_LINES:
        if scores[a] < JOINT_THRESHOLD or scores[b] < JOINT_THRESHOLD:
            continue
        p1 = (int(kpts2d[a][0]), int(kpts2d[a][1]))
        p2 = (int(kpts2d[b][0]), int(kpts2d[b][1]))
        cv2.line(image, p1, p2, (255, 255, 255), 2)

    for i in CORE_JOINTS:
        if scores[i] < JOINT_THRESHOLD:
            continue
        p = (int(kpts2d[i][0]), int(kpts2d[i][1]))
        cv2.circle(image, p, 3, (255, 255, 255), -1)


def draw_standing_area(image, standing_circle):
    cx, cy, radius = standing_circle
    cv2.circle(
        image,
        (int(cx), int(cy)),
        int(radius),
        (0, 255, 255),
        2,
    )
    cv2.circle(
        image,
        (int(cx), int(cy)),
        3,
        (0, 255, 255),
        -1,
    )


def draw_person_center(image, kpts2d, scores):
    center = get_person_center_2d(kpts2d, scores)
    if center is None:
        return
    cv2.circle(
        image,
        (int(center[0]), int(center[1])),
        6,
        (0, 200, 255),
        -1,
    )


def show_calibration(image):
    enlarged = cv2.resize(
        image,
        None,
        fx=CALIBRATION_DISPLAY_SCALE,
        fy=CALIBRATION_DISPLAY_SCALE,
        interpolation=cv2.INTER_LINEAR,
    )
    cv2.imshow("Calibration", enlarged)


# ============================================================
# INFERENCE
# ============================================================

def infer_pose(pose_model, crop, standing_circle=None, use_spatial_lock=True):
    """
    RTMPose3dのみ使用。
    本番もキャリブレーションもYOLOXは使わない。

    standing_circleが渡された場合は、その周囲だけをPose推定に見せる。
    """
    inference_image = crop

    if use_spatial_lock and standing_circle is not None:
        inference_image = apply_spatial_lock_mask(
            crop,
            standing_circle,
        )

    try:
        keypoints3d, scores, keypoints_simcc, keypoints2d = pose_model(
            inference_image
        )

        if len(keypoints2d) == 0:
            return None

        return {
            "keypoints3d": keypoints3d[0],
            "scores": scores[0],
            "simcc": keypoints_simcc[0],
            "keypoints2d": keypoints2d[0],
        }

    except Exception as e:
        print("Pose error:", e)
        return None


def infer_pose_runtime_locked(
    pose_model,
    crop,
    standing_circle,
    use_spatial_lock=True,
):
    """
    Standing Circle人物選択用。runtimeとcalibrationの両方で使用する。

    元のv13のRTMPose3d入力条件は一切変えず、複数人が返った場合だけ
    Standing Circle中心に最も近い人物を選ぶ。

    - キャリブレーションは従来の infer_pose() をそのまま使用
    - YOLO / ByteTrack / bbox crop は使わない
    - confidenceの追加hard gateは入れない
    - 円付近に候補がいなければ他人へ乗り換えず None を返す
    """
    inference_image = crop

    if use_spatial_lock and standing_circle is not None:
        inference_image = apply_spatial_lock_mask(
            crop,
            standing_circle,
        )

    try:
        keypoints3d, scores, keypoints_simcc, keypoints2d = pose_model(
            inference_image
        )

        if len(keypoints2d) == 0:
            return None

        cx, cy, radius = standing_circle
        circle_center = np.array([float(cx), float(cy)], dtype=np.float32)
        max_distance = float(radius) * RUNTIME_PERSON_LOCK_RADIUS_SCALE

        best_index = None
        best_distance = None

        for i in range(len(keypoints2d)):
            center = get_person_center_2d(keypoints2d[i], scores[i])
            if center is None:
                continue

            distance = float(np.linalg.norm(
                np.asarray(center, dtype=np.float32) - circle_center
            ))

            # Circle付近にいない人物は候補にしない。
            # 本人が一瞬取れないときに観客へ乗り換えるのを防ぐ。
            if distance > max_distance:
                continue

            if best_distance is None or distance < best_distance:
                best_index = i
                best_distance = distance

        if best_index is None:
            return None

        return {
            "keypoints3d": keypoints3d[best_index],
            "scores": scores[best_index],
            "simcc": keypoints_simcc[best_index],
            "keypoints2d": keypoints2d[best_index],
        }

    except Exception as e:
        print("Pose error:", e)
        return None


# ============================================================
# SAVE / LOAD
# ============================================================

def save_calibration(data):
    with CALIBRATION_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Calibration saved:", CALIBRATION_FILE.resolve())


def load_calibration():
    if not CALIBRATION_FILE.exists():
        return None

    try:
        with CALIBRATION_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("version") != CALIBRATION_VERSION:
            return None
        if data.get("camera_width") != CAMERA_WIDTH or data.get("camera_height") != CAMERA_HEIGHT:
            print("Calibration resolution mismatch; recalibration required")
            return None
        if len(data.get("right_refs", [])) != CALIBRATION_DIRECTIONS:
            return None
        if len(data.get("left_refs", [])) != CALIBRATION_DIRECTIONS:
            return None
        return data
    except Exception as e:
        print("Could not load calibration:", e)
        return None


# ============================================================
# ROI / CIRCLE SELECTION
# ============================================================

def select_circle(image, window_name="2. Select STANDING CIRCLE"):
    """
    中心から外周までマウスでドラッグして円を指定する。

    Enter / Space : 確定
    R             : やり直し
    Esc           : キャンセル
    """
    state = {
        "center": None,
        "radius": 0,
        "dragging": False,
    }

    def mouse_callback(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            state["center"] = (int(x), int(y))
            state["radius"] = 0
            state["dragging"] = True

        elif event == cv2.EVENT_MOUSEMOVE and state["dragging"]:
            cx, cy = state["center"]
            state["radius"] = int(np.hypot(x - cx, y - cy))

        elif event == cv2.EVENT_LBUTTONUP and state["dragging"]:
            cx, cy = state["center"]
            state["radius"] = int(np.hypot(x - cx, y - cy))
            state["dragging"] = False

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)

    while True:
        preview = image.copy()

        if state["center"] is not None and state["radius"] > 0:
            cv2.circle(
                preview,
                state["center"],
                state["radius"],
                (0, 255, 255),
                2,
            )
            cv2.circle(
                preview,
                state["center"],
                3,
                (0, 255, 255),
                -1,
            )

        cv2.putText(
            preview,
            "Drag CENTER -> EDGE",
            (15, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
        )
        cv2.putText(
            preview,
            "ENTER confirm   R reset   ESC cancel",
            (15, 58),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (255, 255, 255),
            1,
        )

        cv2.imshow(window_name, preview)
        key = cv2.waitKey(20) & 0xFF

        if key in (13, 32):  # Enter / Space
            if state["center"] is not None and state["radius"] >= 5:
                cx, cy = state["center"]
                radius = state["radius"]
                cv2.destroyWindow(window_name)
                return [int(cx), int(cy), int(radius)]

        elif key in (ord("r"), ord("R")):
            state["center"] = None
            state["radius"] = 0
            state["dragging"] = False

        elif key == 27:  # Esc
            cv2.destroyWindow(window_name)
            return None


def select_rois(cap):
    success, frame = cap.read()
    if not success:
        raise RuntimeError("Camera error")

    # 人物を推論する領域は従来通り四角で指定。
    x, y, w, h = cv2.selectROI(
        "1. Select PERSON AREA",
        frame,
        showCrosshair=True,
        fromCenter=False,
    )
    cv2.destroyWindow("1. Select PERSON AREA")
    person_roi = [int(x), int(y), int(w), int(h)]

    if person_roi[2] <= 0 or person_roi[3] <= 0:
        raise RuntimeError("Person ROI was not selected")

    px, py, pw, ph = person_roi
    crop = frame[py:py + ph, px:px + pw].copy()

    # 立ち位置は円で指定。
    standing_circle = select_circle(
        crop,
        "2. Select STANDING CIRCLE",
    )

    if standing_circle is None or standing_circle[2] <= 0:
        raise RuntimeError("Standing circle was not selected")

    return person_roi, standing_circle


# ============================================================
# CALIBRATION OSC
# ============================================================

def send_calibration_direction(osc, side, angle):
    osc.send_message(f"/calibration/{side}", float(angle))


def abort_calibration(osc):
    osc.send_message("/calibration/recording", 0)
    osc.send_message("/calibration/stop", 1)
    print("Calibration aborted: person left standing circle")


# ============================================================
# CALIBRATION ARM QUALITY
# ============================================================

def get_calibration_arm_observation(kpts2d, kpts_simcc, scores, side):
    """
    Calibration-only arm validation.

    Important: do NOT reject the arm because shoulder->wrist is short in 2D.
    When the arm points close to the camera axis, the correct projection can be
    very short.  Instead, judge whether the arm itself is confidently present
    in RTMPose3d and has a plausible 3D length relative to the torso.

    Returns: (obs, ready, reason, arm3d_ratio)
    """
    obs = get_arm_observation(kpts2d, kpts_simcc, scores, side)
    if obs is None:
        return None, False, "ARM NOT FOUND", None

    if float(obs.get("confidence", 0.0)) < CALIBRATION_ARM_CONFIDENCE:
        return obs, False, "LOW WRIST/SHOULDER CONF", None

    shoulder_idx, _, wrist_idx = arm_indices(side)
    torso3d = torso_length_3d(kpts_simcc, scores)
    if torso3d is None or torso3d < 1e-8:
        return obs, False, "NO 3D TORSO", None

    pts3d = simcc_to_normalized_xyz(kpts_simcc)
    arm3d = float(np.linalg.norm(pts3d[wrist_idx] - pts3d[shoulder_idx]))
    arm3d_ratio = arm3d / float(torso3d)

    if arm3d_ratio < CALIBRATION_MIN_ARM_3D_TORSO:
        return obs, False, "3D ARM TOO SHORT", arm3d_ratio
    if arm3d_ratio > CALIBRATION_MAX_ARM_3D_TORSO:
        return obs, False, "3D ARM IMPLAUSIBLE", arm3d_ratio

    # If the elbow is visible, still require the arm to be reasonably extended.
    # If the elbow is occluded, get_arm_observation intentionally falls back to
    # extension=1.0, so camera-axis occlusion does not make calibration impossible.
    if float(obs.get("extension", 0.0)) < CALIBRATION_MIN_EXTENSION:
        return obs, False, "EXTEND ARM", arm3d_ratio

    return obs, True, "READY", arm3d_ratio


# ============================================================
# CALIBRATION HELPERS
# ============================================================

def wait_for_valid_person(cap, pose_model, person_roi, standing_circle, seconds, osc=None, side=None, angle=None, send_cues=False):
    """
    Wait for the prepare/gap period.

    For a directional prepare step, elapsed time alone is never enough to
    advance.  After the timer has elapsed, the requested arm must also be
    confidently detected for several consecutive frames.  This prevents a
    camera-axis direction from being skipped while the wrist is missing.

    Pose loss pauses calibration; it is not treated as "the person left".
    A definite, detected body center outside the standing circle still uses
    CALIBRATION_EXIT_GRACE_SECONDS.
    """
    px, py, pw, ph = person_roi
    start = time.monotonic()
    outside_since = None
    ready_streak = 0

    cue_times = []
    cue_index = 0
    if send_cues:
        if seconds > 0.0:
            cue_times = [0.0, seconds / 3.0, seconds * 2.0 / 3.0]
        else:
            cue_times = [0.0, 0.0, 0.0]

    next_reminder = max(0.0, float(seconds))

    while True:
        now = time.monotonic()
        elapsed = now - start
        remaining = max(0.0, seconds - elapsed)

        success, frame = cap.read()
        if not success:
            continue

        crop = frame[py:py + ph, px:px + pw]
        pose = infer_pose_runtime_locked(
            pose_model, crop, standing_circle, use_spatial_lock=True
        )
        preview = crop.copy()
        draw_standing_area(preview, standing_circle)

        inside = False
        body_detected = False
        ready = False
        reason = "PERSON NOT FOUND"
        arm3d_ratio = None

        if pose is not None:
            k2d = pose["keypoints2d"]
            scores = pose["scores"]
            draw_body(preview, k2d, scores)
            draw_person_center(preview, k2d, scores)

            center = get_person_center_2d(k2d, scores)
            if center is not None:
                body_detected = True
                inside = person_inside_calibration_area(k2d, scores, standing_circle)

            if side is not None and inside:
                _, ready, reason, arm3d_ratio = get_calibration_arm_observation(
                    k2d, pose["simcc"], scores, side
                )
            elif side is None and inside:
                ready = True
                reason = "READY"
            elif body_detected and not inside:
                reason = "PAUSED: RETURN TO CIRCLE"

        # Never abort calibration automatically because of an outside judgment.
        # Pose-center jitter is common from an overhead camera.  If the body is
        # judged outside, simply pause on this direction until it is inside again.
        if body_detected and not inside:
            if outside_since is None:
                outside_since = now
        else:
            outside_since = None

        if elapsed >= seconds and inside and ready:
            ready_streak += 1
        else:
            ready_streak = 0

        required_streak = (
            CALIBRATION_READY_CONSECUTIVE_FRAMES if side is not None else 1
        )
        if ready_streak >= required_streak:
            return True

        if send_cues and osc is not None and side is not None and angle is not None:
            while cue_index < len(cue_times) and elapsed >= cue_times[cue_index]:
                send_calibration_direction(osc, side, angle)
                cue_index += 1

            # If the arm is still not ready after the normal prepare time,
            # keep reminding Max of the SAME direction rather than advancing.
            if elapsed >= next_reminder and elapsed >= seconds:
                send_calibration_direction(osc, side, angle)
                next_reminder = elapsed + CALIBRATION_DIRECTION_REMINDER_SECONDS

        if side is not None and angle is not None:
            cv2.putText(
                preview,
                f"{side.upper()}  {angle:.1f} deg",
                (15, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,255), 2,
            )
            if elapsed < seconds:
                line2 = f"PREPARE {remaining:.1f}s"
                color2 = (0,255,255)
            elif ready:
                line2 = (
                    f"HOLD STEADY {ready_streak}/"
                    f"{CALIBRATION_READY_CONSECUTIVE_FRAMES}"
                )
                color2 = (0,255,0)
            else:
                line2 = f"WAIT: {reason}"
                color2 = (0,0,255)
            cv2.putText(
                preview, line2, (15, 70), cv2.FONT_HERSHEY_SIMPLEX,
                0.68, color2, 2,
            )
            if arm3d_ratio is not None:
                cv2.putText(
                    preview, f"3D arm/torso={arm3d_ratio:.2f}",
                    (15, 106), cv2.FONT_HERSHEY_SIMPLEX,
                    0.52, (255,255,255), 1,
                )
        else:
            cv2.putText(
                preview, f"START IN {remaining:.1f}",
                (15, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,255), 2,
            )
            cv2.putText(
                preview,
                "Stand inside the yellow circle",
                (15, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2,
            )

        show_calibration(preview)
        cv2.waitKey(1)

def capture_calibration_direction(cap, pose_model, person_roi, standing_circle, side, angle, osc):
    # The prepare stage will not finish until the requested arm is actually
    # detected and stable for several consecutive frames.
    ok = wait_for_valid_person(
        cap, pose_model, person_roi, standing_circle,
        CALIBRATION_PREPARE_SECONDS,
        osc=osc, side=side, angle=angle, send_cues=True,
    )
    if not ok:
        abort_calibration(osc)
        return None

    send_calibration_direction(osc, side, angle)
    osc.send_message("/calibration/recording", 1)

    px, py, pw, ph = person_roi
    samples_feature = []
    samples_depth = []
    outside_since = None
    invalid_streak = 0

    while len(samples_feature) < CALIBRATION_SAMPLES:
        now = time.monotonic()
        success, frame = cap.read()
        if not success:
            continue

        crop = frame[py:py + ph, px:px + pw]
        pose = infer_pose_runtime_locked(
            pose_model, crop, standing_circle, use_spatial_lock=True
        )
        preview = crop.copy()
        draw_standing_area(preview, standing_circle)

        inside = False
        body_detected = False
        valid_sample = False
        reason = "PERSON NOT FOUND"
        arm3d_ratio = None

        if pose is not None:
            k2d = pose["keypoints2d"]
            scores = pose["scores"]
            simcc = pose["simcc"]
            draw_body(preview, k2d, scores)
            draw_person_center(preview, k2d, scores)

            center = get_person_center_2d(k2d, scores)
            if center is not None:
                body_detected = True
                inside = person_inside_calibration_area(k2d, scores, standing_circle)

            if inside:
                obs, ready, reason, arm3d_ratio = get_calibration_arm_observation(
                    k2d, simcc, scores, side
                )
                if ready and obs is not None:
                    samples_feature.append(obs["feature2d"].copy())
                    samples_depth.append(float(obs["depth"]))
                    valid_sample = True
                    invalid_streak = 0

        if not valid_sample:
            invalid_streak += 1
            # Do not combine fragments from before and after a tracking failure.
            # If the wrist disappears for a few frames, restart THIS ANGLE's
            # sample block rather than accepting a broken calibration point.
            if (
                invalid_streak >= CALIBRATION_RECORD_INVALID_RESET_FRAMES
                and len(samples_feature) > 0
            ):
                samples_feature.clear()
                samples_depth.clear()
                invalid_streak = 0
                reason = "TRACK LOST - RESTARTING THIS ANGLE"

        if body_detected and not inside:
            # Pause this angle instead of aborting the whole calibration.
            # Samples are not added while outside.
            if outside_since is None:
                outside_since = now
        else:
            outside_since = None

        cv2.putText(
            preview, f"{side.upper()}  {angle:.1f} deg",
            (15, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,255), 2,
        )
        cv2.putText(
            preview,
            f"RECORDING {len(samples_feature)}/{CALIBRATION_SAMPLES}",
            (15, 72), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
            (0,255,0) if valid_sample else (0,255,255), 2,
        )
        if not valid_sample:
            cv2.putText(
                preview, f"WAIT: {reason}",
                (15, 108), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0,0,255), 2,
            )
        if arm3d_ratio is not None:
            cv2.putText(
                preview, f"3D arm/torso={arm3d_ratio:.2f}",
                (15, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (255,255,255), 1,
            )

        show_calibration(preview)
        cv2.waitKey(1)

    osc.send_message("/calibration/recording", 0)

    feature = np.median(np.asarray(samples_feature, dtype=np.float32), axis=0)
    depth = float(np.median(np.asarray(samples_depth, dtype=np.float32)))

    ok = wait_for_valid_person(
        cap, pose_model, person_roi, standing_circle,
        CALIBRATION_GAP_SECONDS,
        osc=None, side=None, angle=None, send_cues=False,
    )
    if not ok:
        abort_calibration(osc)
        return None

    return {
        "angle": float(angle),
        "feature2d": [float(feature[0]), float(feature[1])],
        "depth": depth,
    }

def calibrate_directions(cap, pose_model, person_roi, standing_circle, osc):
    osc.send_message("/calibration/start", 1)

    # 最初だけPCから立ち位置へ移動する時間。ここは足判定で中止しない。
    px, py, pw, ph = person_roi
    start = time.monotonic()
    while time.monotonic() - start < CALIBRATION_START_DELAY:
        success, frame = cap.read()
        if not success:
            continue
        crop = frame[py:py + ph, px:px + pw].copy()
        draw_standing_area(crop, standing_circle)
        remain = CALIBRATION_START_DELAY - (time.monotonic() - start)
        cv2.putText(crop, f"CALIBRATION STARTS IN {max(0, remain):.1f}", (15, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0,255,255), 2)
        cv2.putText(crop, "Move to the standing circle", (15, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
        show_calibration(crop)
        cv2.waitKey(1)

    step = 360.0 / CALIBRATION_DIRECTIONS
    angles = [i * step for i in range(CALIBRATION_DIRECTIONS)]

    right_refs = []
    left_refs = []

    for side, target in (("right", right_refs), ("left", left_refs)):
        for angle in angles:
            print(f"CALIBRATE {side.upper()} {angle:.1f} deg")
            ref = capture_calibration_direction(
                cap, pose_model, person_roi, standing_circle,
                side, angle, osc,
            )
            if ref is None:
                cv2.destroyWindow("Calibration")
                return None
            target.append(ref)

    osc.send_message("/calibration/complete", 1)
    cv2.destroyWindow("Calibration")
    return right_refs, left_refs


def run_full_calibration(cap, pose_model, osc):

    person_roi, standing_circle = select_rois(cap)


    result = calibrate_directions(cap, pose_model, person_roi, standing_circle, osc)
    if result is None:
        return None

    right_refs, left_refs = result

    data = {
        "version": CALIBRATION_VERSION,
        "camera_width": CAMERA_WIDTH,
        "camera_height": CAMERA_HEIGHT,
        "person_roi": person_roi,
        "standing_circle": standing_circle,
        "right_refs": right_refs,
        "left_refs": left_refs,
    }
    save_calibration(data)
    return data


# ============================================================
# OSC-ONLY COMMON ARM SELECTION
# ============================================================

# Pose/azimuth計算には一切使わない。共通 /arm/* をどちらからコピーするかだけ決める。
# 腕を下ろした状態は elevation が大きく負になるため、肘のextensionではなく
# elevationで「操作する高さまで腕が上がっているか」を判定する。
COMMON_ARM_ENTER_ELEVATION = -35.0
COMMON_ARM_EXIT_ELEVATION = -55.0


class CommonArmOscSelector:
    """OSC出力専用。rawのright/left推定値には一切干渉しない。"""

    def __init__(self):
        self.selected = None

    def reset(self):
        self.selected = None

    @staticmethod
    def _usable(v):
        return bool(
            v.get("valid", False)
            and v.get("azimuth") is not None
            and v.get("elevation") is not None
        )

    def select(self, values):
        right = values["right"]
        left = values["left"]

        r_usable = self._usable(right)
        l_usable = self._usable(left)

        r_el_value = right.get("elevation")
        l_el_value = left.get("elevation")
        r_el = -90.0 if r_el_value is None else float(r_el_value)
        l_el = -90.0 if l_el_value is None else float(l_el_value)

        r_enter = r_usable and r_el >= COMMON_ARM_ENTER_ELEVATION
        l_enter = l_usable and l_el >= COMMON_ARM_ENTER_ELEVATION
        r_keep = r_usable and r_el >= COMMON_ARM_EXIT_ELEVATION
        l_keep = l_usable and l_el >= COMMON_ARM_EXIT_ELEVATION

        # すでに選択している側がまだ腕を上げているなら維持。
        # 両腕を出したときにright/leftが毎フレーム切り替わらないためのヒステリシス。
        if self.selected == "right":
            if r_keep:
                return "right"
            if l_enter:
                self.selected = "left"
                return "left"
            self.selected = None
            return None

        if self.selected == "left":
            if l_keep:
                return "left"
            if r_enter:
                self.selected = "right"
                return "right"
            self.selected = None
            return None

        # 未選択。片方だけ上がっていればその側。
        if r_enter and not l_enter:
            self.selected = "right"
        elif l_enter and not r_enter:
            self.selected = "left"
        elif r_enter and l_enter:
            # 同時に両腕を出した場合だけ、より高く上がっている方。
            # 同値ならrightを安定したタイブレークにする。
            self.selected = "right" if r_el >= l_el else "left"
        else:
            self.selected = None

        return self.selected


# ============================================================
# OSC HELPERS
# ============================================================

def send_all_invalid(osc):
    osc.send_message("/arm/right/valid", 0)
    osc.send_message("/arm/left/valid", 0)
    osc.send_message("/arm/valid", 0)
    osc.send_message("/body/valid", 0)


def send_stop(osc):
    osc.send_message("/person/stop", 1)
    send_all_invalid(osc)


def _bundle_add(bundle, address, value):
    msg = OscMessageBuilder(address=address)
    if isinstance(value, bool):
        msg.add_arg(int(value))
    elif isinstance(value, int):
        msg.add_arg(value)
    else:
        msg.add_arg(float(value))
    bundle.add_content(msg.build())


def send_pose_bundle(osc, values, body, common_selector):
    """
    rawの /arm/right/* /arm/left/* は従来どおりそのまま送る。
    共通 /arm/* だけOSC層で片腕を選んでコピーする。
    """
    bundle = OscBundleBuilder(IMMEDIATELY)

    # ---- raw right / left: 一切変更しない ----
    for side in ("right", "left"):
        v = values[side]

        if v["valid"] and v["azimuth"] is not None and v["elevation"] is not None:
            _bundle_add(bundle, f"/arm/{side}/azimuth", v["azimuth"])
            _bundle_add(bundle, f"/arm/{side}/elevation", v["elevation"])
            _bundle_add(bundle, f"/arm/{side}/extension", v["extension"])
            _bundle_add(bundle, f"/arm/{side}/valid", 1)
        else:
            _bundle_add(bundle, f"/arm/{side}/valid", 0)

    # ---- common arm: OSC出力層だけで選択 ----
    selected = common_selector.select(values)
    if selected is not None:
        v = values[selected]
        _bundle_add(bundle, "/arm/azimuth", v["azimuth"])
        _bundle_add(bundle, "/arm/elevation", v["elevation"])
        _bundle_add(bundle, "/arm/extension", v["extension"])
        _bundle_add(bundle, "/arm/valid", 1)
        # 右=1, 左=2。Max側で不要なら無視してよい。
        _bundle_add(bundle, "/arm/side", 1 if selected == "right" else 2)
    else:
        _bundle_add(bundle, "/arm/valid", 0)
        _bundle_add(bundle, "/arm/side", 0)

    if body.get("valid", False) and body.get("azimuth") is not None:
        _bundle_add(bundle, "/body/azimuth", body["azimuth"])
        _bundle_add(bundle, "/body/valid", 1)
    else:
        _bundle_add(bundle, "/body/valid", 0)

    osc.send(bundle.build())


class PoseOscSender:
    """OpenCV表示/推論ループとは独立して最新値を固定レートで送信。"""

    def __init__(self, ip, port, hz):
        self.osc = SimpleUDPClient(ip, port)
        self.interval = 1.0 / float(hz)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active = False
        self.values = {
            "right": {"azimuth": None, "elevation": None, "extension": 0.0, "valid": False},
            "left": {"azimuth": None, "elevation": None, "extension": 0.0, "valid": False},
        }
        self.body = {"azimuth": None, "valid": False}
        self.common_selector = CommonArmOscSelector()
        self.thread = threading.Thread(target=self._run, name="PoseOscSender", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def set_active(self, active):
        with self.lock:
            self.active = bool(active)
            if not self.active:
                self.common_selector.reset()

    def update(self, values, body=None, active=None):
        snap = {}
        for side in ("right", "left"):
            v = values[side]
            snap[side] = {
                "azimuth": v.get("azimuth"),
                "elevation": v.get("elevation"),
                "extension": v.get("extension", 0.0),
                "valid": bool(v.get("valid", False)),
            }

        body_snap = {
            "azimuth": None if body is None else body.get("azimuth"),
            "valid": False if body is None else bool(body.get("valid", False)),
        }

        with self.lock:
            self.values = snap
            self.body = body_snap
            if active is not None:
                self.active = bool(active)

    def invalidate(self):
        with self.lock:
            for side in ("right", "left"):
                self.values[side]["valid"] = False
            self.body["valid"] = False
            self.common_selector.reset()

    def _run(self):
        next_send = time.perf_counter()
        while not self.stop_event.is_set():
            now = time.perf_counter()
            if now < next_send:
                self.stop_event.wait(next_send - now)
                continue

            if now - next_send > self.interval * 2.0:
                next_send = now

            with self.lock:
                active = self.active
                values = {side: dict(self.values[side]) for side in ("right", "left")}
                body = dict(self.body)

            if active:
                send_pose_bundle(self.osc, values, body, self.common_selector)

            next_send += self.interval


# ============================================================
# MAIN
# ============================================================

def main():

    cv2.setUseOptimized(True)
    set_windows_above_normal_priority()
    set_windows_high_qos()

    # start/stop/calibration用
    osc = SimpleUDPClient(OSC_IP, OSC_PORT)

    # 腕 + body orientationの連続値は別スレッドで固定送信
    pose_sender = PoseOscSender(OSC_IP, OSC_PORT, OSC_SEND_HZ)
    pose_sender.start()

    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)

    # 720p/30fpsを要求。多くのUSB WebカメラではMJPG指定の方が
    # 1280x720を30fpsで安定して取得しやすい。
    if CAMERA_USE_MJPG:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    # 古いフレームを溜めない。DirectShowでは無視される環境もあるが安全。
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    success, _ = cap.read()
    if not success:
        raise RuntimeError("Webcam error")

    actual_w = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
    actual_h = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    actual_fps = float(cap.get(cv2.CAP_PROP_FPS))
    print(f"Camera requested: {CAMERA_WIDTH}x{CAMERA_HEIGHT} @ {CAMERA_FPS:.0f}fps")
    print(f"Camera actual:    {actual_w}x{actual_h} @ {actual_fps:.1f}fps")
    if actual_w != CAMERA_WIDTH or actual_h != CAMERA_HEIGHT:
        print("WARNING: camera did not accept 1280x720; check webcam-supported modes")


    print("Loading RTMW3D...")
    pose_model = RTMPose3d(
        onnx_model=RTMW3D_MODEL,
        model_input_size=MODEL_INPUT_SIZE,
        backend="onnxruntime",
        device="cpu",
        to_openpose=False,
    )
    print("RTMW3D ready")
    print("Calibration mode: Spatial Lock + RTMPose3d only")
    print("Runtime mode: v13 core + Standing Circle person lock (NO YOLOX)")
    print("Background-stable mode: HighQoS requested")

    calibration = load_calibration()
    if calibration is None:
        calibration = run_full_calibration(cap, pose_model, osc)
        if calibration is None:
            print("Calibration stopped. Exiting.")
            cap.release()
            cv2.destroyAllWindows()
            return
    else:
        print("Loaded:", CALIBRATION_FILE.resolve())

    person_roi = calibration["person_roi"]
    standing_circle = calibration["standing_circle"]
    right_refs = calibration["right_refs"]
    left_refs = calibration["left_refs"]


    body_direction_refs = build_direction_refs(right_refs, left_refs)
    body_azimuth = None
    body_values = {"azimuth": None, "valid": False}

    states = {
        "right": AzimuthState(),
        "left": AzimuthState(),
    }

    smoothed_elevation = {
        "right": None,
        "left": None,
    }

    last_values = {
        "right": {"azimuth": None, "elevation": None, "extension": 0.0, "valid": False, "axis": False},
        "left": {"azimuth": None, "elevation": None, "extension": 0.0, "valid": False, "axis": False},
    }
    # 左右選択はデバッグのため一旦完全に切り離す。
    output_values = {"right": dict(last_values["right"]), "left": dict(last_values["left"])}

    active = False
    inside_since = None
    outside_since = None
    room_up = None
    up_samples = []
    waiting_elapsed = 0.0
    frame_count = 0
    last_pose = None
    person_inside = False

    print("q = quit")
    print("r = recalibrate all")

    try:
        while cap.isOpened():
            success, frame = cap.read()
            if not success:
                break

            frame_count += 1
            px, py, pw, ph = person_roi
            crop = frame[py:py + ph, px:px + pw]

            if frame_count % PROCESS_EVERY_N_FRAMES == 0:
                pose = infer_pose_runtime_locked(
                    pose_model, crop, standing_circle, use_spatial_lock=True
                )
                now = time.monotonic()

                if pose is not None:
                    last_pose = pose
                    k2d = pose["keypoints2d"]
                    scores = pose["scores"]
                    simcc = pose["simcc"]

                    person_inside = person_inside_standing_area(k2d, scores, standing_circle)

                    # ---------------- WAITING / START ----------------
                    if not active:
                        outside_since = None
                        if person_inside:
                            if inside_since is None:
                                inside_since = now
                                up_samples = []

                            up_now = get_body_up(simcc, scores)
                            if up_now is not None:
                                up_samples.append(up_now)
                                if len(up_samples) > 60:
                                    up_samples.pop(0)

                            waiting_elapsed = now - inside_since

                            if waiting_elapsed >= ENTER_SECONDS:
                                if up_samples:
                                    room_up = normalize(np.mean(np.asarray(up_samples), axis=0))
                                else:
                                    room_up = get_body_up(simcc, scores)

                                if room_up is not None:
                                    active = True
                                    for state in states.values():
                                        state.reset()
                                    smoothed_elevation = {"right": None, "left": None}
                                    output_values = {"right": dict(last_values["right"]), "left": dict(last_values["left"])}
                                    body_azimuth = None
                                    body_values = {"azimuth": None, "valid": False}
                                    osc.send_message("/person/start", 1)
                                    pose_sender.update(output_values, body_values, active=True)
                                    print(">>> START")
                        else:
                            inside_since = None
                            waiting_elapsed = 0.0
                            up_samples = []

                    # ---------------- ACTIVE / STOP ----------------
                    else:
                        if person_inside:
                            outside_since = None
                        else:
                            send_all_invalid(osc)
                            last_values["right"]["valid"] = False
                            last_values["left"]["valid"] = False

                            if outside_since is None:
                                outside_since = now

                            if now - outside_since >= EXIT_SECONDS:
                                active = False
                                inside_since = None
                                outside_since = None
                                room_up = None
                                up_samples = []
                                waiting_elapsed = 0.0
                                for state in states.values():
                                    state.reset()
                                    output_values = {"right": dict(last_values["right"]), "left": dict(last_values["left"])}
                                body_azimuth = None
                                body_values = {"azimuth": None, "valid": False}
                                pose_sender.invalidate()
                                pose_sender.set_active(False)
                                send_stop(osc)
                                print("<<< STOP")

                    # ---------------- BODY ORIENTATION ----------------
                    if active and person_inside:
                        body_dir = get_body_facing_direction_2d(
                            k2d,
                            scores,
                            body_azimuth,
                            body_direction_refs,
                        )
                        raw_body_azimuth = map_image_direction_to_world(
                            body_dir, body_direction_refs
                        ) if body_dir is not None else None

                        if raw_body_azimuth is not None:
                            body_azimuth = smooth_body_angle(
                                body_azimuth, raw_body_azimuth
                            )
                            body_values = {
                                "azimuth": body_azimuth,
                                "valid": True,
                            }
                        else:
                            body_values["valid"] = False

                    # ---------------- ARMS ----------------
                    # 安定していたv13と同じ: 左右を独立してそのまま更新する。
                    # 「どちらの腕を選ぶか」はここでは一切判定しない。
                    if active and person_inside and room_up is not None:
                        for side, refs in (("right", right_refs), ("left", left_refs)):
                            obs = get_arm_observation(k2d, simcc, scores, side)
                            valid = False

                            if (
                                obs is not None
                                and obs["length_px"] >= MIN_ARM_2D_PIXELS
                                and obs["extension"] >= MIN_EXTENSION
                            ):
                                azimuth, feature_error, axis_zone = states[side].update(
                                    obs["feature2d"],
                                    obs["depth"],
                                    refs,
                                    now,
                                )

                                raw_elevation = calculate_elevation(
                                    simcc, scores, side, room_up
                                )

                                if azimuth is not None and raw_elevation is not None:
                                    elevation = smooth_scalar_adaptive(
                                        smoothed_elevation[side],
                                        raw_elevation,
                                    )
                                    smoothed_elevation[side] = elevation
                                    valid = True

                                    last_values[side] = {
                                        "azimuth": azimuth,
                                        "elevation": elevation,
                                        "extension": obs["extension"],
                                        "valid": True,
                                        "axis": axis_zone,
                                    }

                            if not valid:
                                states[side].mark_invalid(now)
                                last_values[side]["valid"] = False

                        output_values = {
                            "right": dict(last_values["right"]),
                            "left": dict(last_values["left"]),
                        }
                        pose_sender.update(output_values, body_values, active=True)

                else:
                    # ロック対象が見つからない = 完全に退出した可能性がある。
                    # 以前はここでoutside_sinceを進めていなかったため、
                    # 人物が完全に円/画面から消えると /person/stop が送られなかった。
                    person_inside = False
                    if active:
                        last_values["right"]["valid"] = False
                        last_values["left"]["valid"] = False
                        output_values = {"right": dict(last_values["right"]), "left": dict(last_values["left"])}
                        body_values["valid"] = False
                        pose_sender.update(output_values, body_values, active=True)

                        if outside_since is None:
                            outside_since = now

                        if now - outside_since >= EXIT_SECONDS:
                            active = False
                            inside_since = None
                            outside_since = None
                            room_up = None
                            up_samples = []
                            waiting_elapsed = 0.0
                            for state in states.values():
                                state.reset()
                            output_values = {
                                "right": dict(last_values["right"]),
                                "left": dict(last_values["left"]),
                            }
                            body_azimuth = None
                            body_values = {"azimuth": None, "valid": False}
                            pose_sender.invalidate()
                            pose_sender.set_active(False)
                            send_stop(osc)
                            print("<<< STOP (person left / pose missing)")

            # =================================================
            # PREVIEW
            # =================================================
            if SHOW_PREVIEW:
                preview = crop.copy()
                draw_standing_area(preview, standing_circle)

                # Pose推定に見せているSpatial Lock範囲（薄い円）
                if SPATIAL_LOCK_ENABLED:
                    sx, sy, sr = standing_circle
                    cv2.circle(
                        preview,
                        (int(sx), int(sy)),
                        int(sr * SPATIAL_LOCK_RADIUS_SCALE),
                        (100, 100, 100),
                        1,
                    )

                    lock_radius = int(sr * SPATIAL_LOCK_RADIUS_SCALE)
                    label_x = max(10, int(sx - lock_radius))
                    label_y = max(20, int(sy - lock_radius - 8))

                    cv2.putText(
                        preview,
                        "TARGET LOCK",
                        (label_x, label_y),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.48,
                        (0, 255, 0),
                        1,
                    )

                if last_pose is not None:
                    draw_body(preview, last_pose["keypoints2d"], last_pose["scores"])
                    draw_person_center(preview, last_pose["keypoints2d"], last_pose["scores"])

                if active:
                    status = "ACTIVE"
                    status_color = (0, 255, 0)
                elif person_inside:
                    remain = max(0.0, ENTER_SECONDS - waiting_elapsed)
                    status = f"START IN {remain:.1f}s"
                    status_color = (0, 255, 255)
                else:
                    status = "WAITING"
                    status_color = (0, 0, 255)

                cv2.putText(preview, status, (15, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, status_color, 2)

                cv2.putText(
                    preview,
                    "ARMS: RAW INDEPENDENT (selection OFF)",
                    (15, 54),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.48,
                    (0, 255, 255),
                    1,
                )

                y = 78
                for side in ("right", "left"):
                    v = output_values[side]
                    if v["azimuth"] is None:
                        text = f"{side}: ---"
                    else:
                        axis_text = " AXIS" if v.get("axis") else ""
                        text = (
                            f"{side}: az={v['azimuth']:6.1f} "
                            f"el={v['elevation']:6.1f} "
                            f"ext={v['extension']:.2f} "
                            
                            f"{'ON' if v['valid'] else 'OFF'}{axis_text}"
                        )
                    cv2.putText(preview, text, (15, y), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255,255,255), 1)
                    y += 24

                if body_values.get("valid") and body_values.get("azimuth") is not None:
                    body_text = f"body: az={body_values['azimuth']:6.1f} ON"
                else:
                    body_text = "body: --- OFF"
                cv2.putText(preview, body_text, (15, y), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255,255,255), 1)

                cv2.putText(preview, "Q quit   R recalibrate", (15, preview.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255,255,255), 1)
                cv2.imshow("One Camera Hybrid Pose -> OSC", preview)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break

            if key == ord("r"):
                if active:
                    pose_sender.invalidate()
                    pose_sender.set_active(False)
                    send_stop(osc)
                active = False
                inside_since = None
                outside_since = None
                room_up = None
                up_samples = []
                for state in states.values():
                    state.reset()
                output_values = {
                    "right": dict(last_values["right"]),
                    "left": dict(last_values["left"]),
                }
                body_azimuth = None
                body_values = {"azimuth": None, "valid": False}

                cv2.destroyWindow("One Camera Hybrid Pose -> OSC")
                calibration = run_full_calibration(cap, pose_model, osc)
                if calibration is None:
                    print("Calibration stopped; keeping previous calibration")
                else:
                    person_roi = calibration["person_roi"]
                    standing_circle = calibration["standing_circle"]
                    right_refs = calibration["right_refs"]
                    left_refs = calibration["left_refs"]
                    body_direction_refs = build_direction_refs(right_refs, left_refs)
                    body_azimuth = None
                    body_values = {"azimuth": None, "valid": False}
                    last_pose = None

    finally:
        pose_sender.invalidate()
        pose_sender.set_active(False)
        pose_sender.stop()

        if active:
            send_stop(osc)
        else:
            send_all_invalid(osc)
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
