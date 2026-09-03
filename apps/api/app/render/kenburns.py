"""Stage A2: a still image becomes a moving clip.

`zoompan` positions the crop window on integer pixels, which produces visible
stepping on slow moves. Rendering at 2x and scaling down hides the quantisation
-- it costs encode time and buys the difference between "a slideshow" and "a
camera move".
"""
from __future__ import annotations

from .timeline import CameraMove, KenBurns

#: Fixed zoom used by pans and tilts: the move needs slack inside the frame to
#: travel across, which only exists if we are already zoomed in.
TRAVEL_ZOOM = 1.18

#: Moves that are approximated rather than reproduced. Ken Burns cannot orbit;
#: the preview's job is to rehearse pacing, not to fake parallax.
_APPROXIMATED = {
    CameraMove.ORBIT: "slow push (orbit needs real 3-D motion)",
    CameraMove.HANDHELD: "very slow push (no jitter without a noise source)",
}


def describe(move: CameraMove) -> str:
    return _APPROXIMATED.get(move, "")


def _progress(frames: int) -> str:
    """0..1 across the clip, guarding the single-frame case."""
    return f"on/{max(1, frames - 1)}"


def zoompan_expr(kb: KenBurns, frames: int) -> tuple[str, str, str]:
    """Return (z, x, y) expressions for one Ken Burns move."""
    p = _progress(frames)
    zs, ze = kb.start_scale, kb.end_scale
    centre_x = "iw/2-(iw/zoom/2)"
    centre_y = "ih/2-(ih/zoom/2)"

    match kb.move:
        case CameraMove.STATIC:
            return "1.0", centre_x, centre_y
        case CameraMove.PUSH_IN | CameraMove.ORBIT | CameraMove.HANDHELD:
            speed = 0.35 if kb.move is CameraMove.HANDHELD else 1.0
            return (f"min({zs}+({ze}-{zs})*{speed}*{p},{max(zs, ze)})",
                    centre_x, centre_y)
        case CameraMove.PULL_OUT:
            return (f"max({ze}-({ze}-{zs})*{p},{min(zs, ze)})",
                    centre_x, centre_y)
        case CameraMove.PAN_LEFT:
            return (f"{TRAVEL_ZOOM}", f"(iw-iw/zoom)*(1-{p})", centre_y)
        case CameraMove.PAN_RIGHT:
            return (f"{TRAVEL_ZOOM}", f"(iw-iw/zoom)*{p}", centre_y)
        case CameraMove.TILT_UP:
            return (f"{TRAVEL_ZOOM}", centre_x, f"(ih-ih/zoom)*(1-{p})")
        case CameraMove.TILT_DOWN:
            return (f"{TRAVEL_ZOOM}", centre_x, f"(ih-ih/zoom)*{p}")
    return "1.0", centre_x, centre_y


def filter_chain(
    kb: KenBurns, *, width: int, height: int, fps: int, duration_ms: int,
    supersample: int = 2,
) -> str:
    """Full filter chain: still -> uniform intermediate frame stream.

    Stills are scaled to *cover* and cropped rather than padded: the approved
    still already matches the project aspect, and a moving image with black
    bars looks like a mistake.
    """
    frames = max(1, round(duration_ms / 1000 * fps))
    sw, sh = width * supersample, height * supersample
    z, x, y = zoompan_expr(kb, frames)
    return (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={sw}:{sh},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={sw}x{sh}:fps={fps},"
        f"scale={width}:{height}:flags=lanczos,"
        f"format=yuv420p,setsar=1"
    )
