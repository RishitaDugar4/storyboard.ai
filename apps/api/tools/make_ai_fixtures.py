#!/usr/bin/env python3
"""Author the golden AI fixtures by constructing the models directly.

Building through Pydantic rather than hand-writing JSON means a fixture cannot
be checked in that the schema would reject -- and when a validator changes, this
script fails loudly instead of leaving a stale file that silently no longer
represents valid output.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "apps" / "api"))

from app.schemas.ai import (Beat, CharacterCanon, DetectedCharacter,  # noqa: E402
                            DetectedLocation, LocationCanon, NarrationLine,
                            Scene, Shot, StoryAnalysis, Storyboard, StyleBible,
                            VoiceProfile)

OUT = REPO / "apps" / "api" / "tests" / "fixtures" / "ai"

analysis = StoryAnalysis(
    title="The Lighthouse Keeper",
    logline=("An ageing keeper turns a lighthouse lens by hand through a "
             "blackout storm so that eleven men she will never meet can find "
             "harbour."),
    tone=["wistful", "spare", "quietly heroic", "elegiac"],
    setting_summary=("Corrow Point, a remote island lighthouse, across forty-one "
                     "winters and culminating in one blacked-out night storm."),
    characters=[
        DetectedCharacter(
            name="Mara Halloran", aliases=["Mara", "the keeper"], role="protagonist",
            description_from_text=("Kept the light on Corrow Point for forty-one "
                                  "winters; seventy-three in the year of the "
                                  "blackout; writes the weather in a ledger "
                                  "nobody reads."),
            first_mention_excerpt=("For forty-one winters, Mara Halloran kept the "
                                   "light on Corrow Point burning.")),
        DetectedCharacter(
            name="The captain of the Bright Endeavour", role="supporting",
            description_from_text=("Steered by a light that swung strangely, "
                                   "slower than it should, but never stopped."),
            first_mention_excerpt=("Their captain said afterwards that he had "
                                   "steered by a light that swung strangely.")),
        DetectedCharacter(
            name="The interviewer", aliases=["the girl"], role="incidental",
            description_from_text="A girl who came to interview her after the medal.",
            first_mention_excerpt=("she told the girl who came to interview her "
                                   "that the medal had it wrong")),
    ],
    locations=[
        DetectedLocation(name="Corrow Point lighthouse", interior=True,
                         description_from_text=("Two hundred and eleven steps to "
                                                "the lamp room; a clockwork lens; "
                                                "a kitchen with one electric bulb.")),
        DetectedLocation(name="The sea off Corrow Point", interior=False,
                         description_from_text=("Flat grey in August, black and "
                                                "hammering in February, strange "
                                                "green calm before the worst.")),
        DetectedLocation(name="A hall in the town", interior=True,
                         description_from_text="Where they gave her a medal the following spring."),
    ],
    beats=[
        Beat(index=0, emotional_valence="setup",
             summary="Mara has kept the light for forty-one winters, unasked.",
             source_excerpt="in all that time nobody ever asked her to"),
        Beat(index=1, emotional_valence="setup",
             summary="She learns the sea's moods like a language.",
             source_excerpt="the flat grey patience of August, the black hammering of February"),
        Beat(index=2, emotional_valence="setup",
             summary="Her routine: the wick, the clockwork, a ledger nobody reads.",
             source_excerpt="She wrote the weather in a ledger nobody read"),
        Beat(index=3, emotional_valence="rising",
             summary="At seventy-three, the island loses all power.",
             source_excerpt="In the winter she turned seventy-three, the light went out."),
        Beat(index=4, emotional_valence="turn",
             summary="She understands the ships cannot see her.",
             source_excerpt="the ships out there could not see her"),
        Beat(index=5, emotional_valence="rising",
             summary="She climbs 211 steps in the dark with oil and matches.",
             source_excerpt="Two hundred and eleven steps with a can of oil and a box of matches"),
        Beat(index=6, emotional_valence="rising",
             summary="She lights the old lamp by hand, as the first keeper did in 1868.",
             source_excerpt="the way the first keeper had in 1868"),
        Beat(index=7, emotional_valence="climax",
             summary="She turns the lens with her own arms all night.",
             source_excerpt="turning the lens with her own arms because the clockwork needed power she did not have"),
        Beat(index=8, emotional_valence="climax",
             summary="Her shoulders give out near four; she keeps turning, counting aloud.",
             source_excerpt="counting rotations aloud to stay awake"),
        Beat(index=9, emotional_valence="resolution",
             summary="The Bright Endeavour makes harbour at seven with eleven men.",
             source_excerpt="The trawler Bright Endeavour made harbour at seven. Eleven men aboard."),
        Beat(index=10, emotional_valence="resolution",
             summary="A medal in spring, which she says has it wrong.",
             source_excerpt="the medal had it wrong. It was not brave, she said."),
        Beat(index=11, emotional_valence="resolution",
             summary="She had simply never considered letting it go out.",
             source_excerpt="She had simply never considered letting it go out."),
    ],
)

style = StyleBible(
    art_style=("Soft gouache storybook illustration with visible brush texture "
               "and simplified, graphic shapes"),
    palette=["slate grey", "lamp amber", "deep sea green", "bone white", "storm indigo"],
    lighting="A single warm light source against cold ambient blue",
    camera_language="Patient, mostly locked-off framing; movement only when the story turns",
    line_and_texture="Soft edges, dry-brush grain, no fine linework",
    motion_language="Gentle, unhurried camera movement; nothing whips or snaps",
    negative=["photorealism", "harsh outlines", "lens flare", "text", "watermark"],
)

mara = CharacterCanon(
    slug="mara", name="Mara Halloran", role="keeper",
    age_impression="early seventies", build="small and wiry, slightly stooped",
    hair="white, cropped short, wind-disordered",
    eyes="pale grey, deep-set, steady",
    skin="weathered and wind-reddened, freckled across the nose",
    distinguishing_features=["knuckles swollen from cold",
                             "a habitual squint against wind"],
    default_wardrobe=("a heavy oilskin coat over a fisherman's jumper, "
                      "fingerless wool gloves"),
    voice=VoiceProfile(age_range="elderly", timbre="dry and warm",
                       pace="slow", accent="coastal"),
)

locations = [
    LocationCanon(slug="lamp-room", name="The lamp room",
                  description="The glass chamber at the top of the tower housing the lens",
                  prompt_fragment=("a cramped glass lamp room, brass fittings, a huge "
                                   "faceted lens, storm-black windows")),
    LocationCanon(slug="stair", name="The tower stair",
                  description="A tight spiral of 211 stone steps",
                  prompt_fragment="a narrow spiral stone staircase, worn treads, deep shadow"),
    LocationCanon(slug="kitchen", name="The keeper's kitchen",
                  description="A small room with one electric bulb and a ledger on the table",
                  prompt_fragment="a small spare kitchen, scrubbed table, an open ledger"),
    LocationCanon(slug="sea", name="The sea off Corrow Point",
                  description="Open water, changing with the season",
                  prompt_fragment="open grey-green sea under a low sky, no land in sight"),
]

# (title, summary, location, time, mood, shot_type, move, action, motion,
#  priority, duration, narration)
PLAN = [
    ("The light", "Establishing the lighthouse across decades", "sea", "dusk",
     "elegiac", "establishing", "push_in",
     "The lighthouse stands alone on a black rock as dusk falls over open water",
     "The beam sweeps once across the water; waves move slowly below",
     "high", 8.0, "For forty-one winters, Mara kept the light burning."),
    ("Nobody asked", "Her solitude, stated plainly", "kitchen", "night",
     "spare", "medium", "static",
     "Mara sits alone at a scrubbed table writing in a ledger by one dim bulb",
     "Her hand moves steadily across the page; the bulb flickers faintly",
     "low", 6.0, "Nobody had ever asked her to."),
    ("The moods of the sea", "She knows the water like a language", "sea", "day",
     "watchful", "wide", "pan_right",
     "Flat grey water stretching to a pale horizon under high cloud",
     "Light moves slowly across the surface of the water",
     "low", 6.0, "She learned the sea the way others learn a language."),
    ("February", "The sea at its worst", "sea", "night", "violent", "wide",
     "static", "Black water hammering against rock under a starless sky",
     "Spray bursts upward and falls; the water heaves",
     "medium", 5.0, "Black hammering in February."),
    ("The ledger", "Her small unread record", "kitchen", "night", "quiet",
     "insert", "push_in",
     "A close view of a ledger page filled with tiny handwriting and weather notes",
     "The page edge lifts slightly in a draught",
     "low", 6.0, "She wrote the weather in a ledger nobody read."),
    ("The blackout", "All power fails", "kitchen", "night", "sudden", "medium",
     "static", "Mara sits in total darkness, the bulb above her dead",
     "Nothing moves but her breath; darkness settles",
     "medium", 5.0, "In her seventy-third winter, the island went dark."),
    ("They cannot see her", "The realisation", "kitchen", "night", "dread",
     "close_up", "push_in",
     "Mara's face in near darkness, turned toward the sound of the storm",
     "Her eyes shift slightly; her jaw tightens",
     "high", 6.0, "Out there, the ships could not see her."),
    ("The climb", "211 steps in the dark", "stair", "night", "resolute",
     "wide", "tilt_up",
     "Mara climbs a narrow spiral stair carrying an oil can and matches",
     "She climbs steadily upward, one hand on the wall",
     "high", 8.0, "So she climbed. Two hundred and eleven steps."),
    ("Lit by hand", "The old lamp, the old way", "lamp-room", "night", "reverent",
     "medium", "push_in",
     "Mara touches a lit match to the wick of a large brass oil lamp",
     "The flame catches and grows; warm light spreads across brass",
     "high", 6.0, "She lit it by hand, as the first keeper had."),
    ("Turning", "She becomes the clockwork", "lamp-room", "night", "straining",
     "medium", "orbit",
     "Mara leans into a handle, turning the great lens by her own strength",
     "The lens rotates slowly; her shoulders work against the weight",
     "high", 8.0, "The clockwork needed power she did not have."),
    ("Four in the morning", "Past the end of her strength", "lamp-room", "night",
     "exhausted", "close_up", "static",
     "Mara's stiff hands gripping the handle, knuckles white",
     "Her fingers tremble but do not release",
     "medium", 6.0, "Her shoulders gave out. She kept turning."),
    ("Bright Endeavour", "The trawler finds harbour", "sea", "dawn", "relief",
     "wide", "pull_out",
     "A trawler moves through grey dawn water toward a harbour mouth",
     "The boat moves steadily forward through low swell",
     "high", 8.0, "At seven, eleven men made harbour."),
    ("Not brave", "Her correction", "kitchen", "day", "plain", "close_up",
     "static", "Mara sits upright, looking directly ahead, unimpressed",
     "She blinks once, slowly",
     "medium", 6.0, "It was not brave, she said. Bravery is decided."),
    ("Never considered", "The last line", "sea", "dusk", "elegiac",
     "establishing", "pull_out",
     "The lighthouse small against a wide darkening sea, its beam turning",
     "The beam completes one slow sweep across the water",
     "high", 8.0, "She had never considered letting it go out."),
]

scenes = []
for i, (title, summary, loc, tod, mood, stype, move, action, motion,
        prio, dur, line) in enumerate(PLAN):
    subjects = ["mara"] if "Mara" in action else []
    scenes.append(Scene(
        local_index=i, title=title, summary=summary, location_slug=loc,
        present_slugs=subjects, time_of_day=tod, mood=mood,
        shots=[Shot(local_index=0, shot_type=stype, subject_slugs=subjects,
                    action=action, camera_move=move, subject_motion=motion,
                    motion_priority=prio, target_duration_s=dur,
                    ambient_sound="wind and sea")],
        narration=[NarrationLine(local_index=0, shot_local_index=0,
                                 speaker="narrator", text=line,
                                 delivery="wistful")],
    ))

storyboard = Storyboard(
    title="The Lighthouse Keeper",
    logline=analysis.logline,
    style_bible=style, characters=[mara], locations=locations, scenes=scenes,
)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "story_analysis.json").write_text(analysis.model_dump_json(indent=2))
    (OUT / "storyboard.json").write_text(storyboard.model_dump_json(indent=2))
    print(f"  story_analysis.json  {len(analysis.characters)} characters, "
          f"{len(analysis.beats)} beats")
    print(f"  storyboard.json      {len(storyboard.scenes)} scenes, "
          f"{storyboard.shot_count} shots, "
          f"{storyboard.total_target_duration_s:.0f}s target runtime")
    over = [(sc.local_index, sh.local_index) for sc in storyboard.scenes
            for sh in sc.shots
            if sum(n.word_count for n in sc.narration) > int((sh.target_duration_s - 0.9) * 2.5)]
    print(f"  narration overflows  {len(over)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
