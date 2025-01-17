"""Test validation methods."""

from __future__ import annotations

from copy import copy, deepcopy
from pathlib import Path
from typing import Any

from symusic import (
    Note,
    Pedal,
    Score,
    Tempo,
    TextMeta,
    TimeSignature,
    Track,
)
from symusic.core import TempoTickList

import miditok
from miditok.constants import CHORD_MAPS, TIME_SIGNATURE, TIME_SIGNATURE_RANGE

SEED = 777

HERE = Path(__file__).parent
MIDI_PATHS_ONE_TRACK = sorted((HERE / "MIDIs_one_track").rglob("*.mid"))
MIDI_PATHS_MULTITRACK = sorted((HERE / "MIDIs_multitrack").rglob("*.mid"))
MIDI_PATHS_ALL = sorted(
    deepcopy(MIDI_PATHS_ONE_TRACK) + deepcopy(MIDI_PATHS_MULTITRACK)
)
TEST_LOG_DIR = HERE / "test_logs"
# MIDI files known to contain tricky contents (time sig, pedals...) and edge case
# situations, likely to make some tests fail.
MIDIS_ONE_TRACK_HARD_NAMES = [
    "6338816_Etude No. 4.mid",
    "6354774_Macabre Waltz.mid",
    "Maestro_9.mid",
    "POP909_191.mid",
]
MIDI_PATHS_ONE_TRACK_HARD = [
    path for path in MIDI_PATHS_ONE_TRACK if path.name in MIDIS_ONE_TRACK_HARD_NAMES
]

# TOKENIZATIONS
ALL_TOKENIZATIONS = miditok.tokenizations.__all__
TOKENIZATIONS_BPE = ["REMI", "MIDILike", "TSD", "MMM", "Structured"]

# TOK CONFIG PARAMS
TIME_SIGNATURE_RANGE_TESTS = TIME_SIGNATURE_RANGE
TIME_SIGNATURE_RANGE_TESTS.update({2: [2, 3, 4]})
TIME_SIGNATURE_RANGE_TESTS[4].append(8)
TOKENIZER_CONFIG_KWARGS = {
    "special_tokens": ["PAD", "BOS_None", "EOS", "EOS_test_None"],
    "beat_res": {(0, 4): 8, (4, 12): 4, (12, 16): 2},
    "beat_res_rest": {(0, 2): 4, (2, 12): 2},
    "num_tempos": 32,
    "tempo_range": (40, 250),
    "time_signature_range": TIME_SIGNATURE_RANGE_TESTS,
    "chord_maps": CHORD_MAPS,
    "chord_tokens_with_root_note": True,  # Tokens will look as "Chord_C:maj"
    "chord_unknown": (3, 6),
    "delete_equal_successive_time_sig_changes": True,
    "delete_equal_successive_tempo_changes": True,
}


def adjust_tok_params_for_tests(tokenization: str, params: dict[str, Any]) -> None:
    """Adjusts parameters (as dictionary for keyword arguments) depending on the
    tokenization.

    :param tokenization: tokenization.
    :param params: parameters as a dictionary of keyword arguments.
    """
    # Increase the TimeShift voc for Structured as it doesn't support successive
    # TimeShifts.
    if tokenization == "Structured":
        params["beat_res"] = {(0, 512): 8}
    # We don't test time signatures with Octuple as it can lead to time shifts, as the
    # TS changes are only carried at the onset times of the notes.
    elif tokenization == "Octuple":
        params["max_bar_embedding"] = 300
        params["use_time_signatures"] = False
    # Rests and time sig can mess up with CPWord, when a Rest that is crossing new bar
    # is followed by a new TimeSig change, as TimeSig are carried with Bar tokens (and
    # there is None is this case).
    elif (
        tokenization == "CPWord"
        and params.get("use_time_signatures", False)
        and params.get("use_rests", False)
    ):
        params["use_rests"] = False


def sort_midi(midi: Score, sort_tracks: bool = True) -> None:
    """Sorts a MIDI: its notes and other track events, and the tracks themselves.

    :param midi: midi to sort.
    :param sort_tracks: will sort the tracks by program if given True.
    """
    for track in midi.tracks:
        if track.is_drum:
            track.program = 0  # need to be done before sorting tracks per program
        track.notes.sort(key=lambda x: (x.time, x.pitch, x.duration, x.velocity))
        # track.pedals.sort()
        # track.pitch_bends.sort()
        # track.controls.sort()

    # Sorts tracks
    # MIDI detokenized with one_token_stream contains tracks sorted by note occurrence
    # This is done at the end as we may
    if sort_tracks:
        midi.tracks.sort(key=lambda x: (x.program, x.is_drum))


def adapt_ref_midi_before_tokenize(
    midi: Score, tokenizer: miditok.MIDITokenizer
) -> None:
    """Adapt (inplace) the contents of a MIDI before it is tokenized.

    :param midi: MIDI object to adapt.
    :param tokenizer: tokenizer being used.
    """
    tokenization = type(tokenizer).__name__ if tokenizer is not None else None

    if tokenizer._note_on_off:
        # Need to sort the notes with all these keys, as otherwise some velocity values
        # might be mixed up for notes with the same onset and duration values as the
        # tokens are decoded in a FIFO logic.
        # But before sorting, we need to merge the tracks if needed, and clip durations
        if tokenizer.config.use_programs and tokenizer.one_token_stream:
            miditok.utils.merge_same_program_tracks(midi.tracks)

        # If a max_duration is provided, we clip the durations of the notes before
        # tokenizing, otherwise these notes will be tokenized with durations > to this
        # limit, which would yield errors when checking TSE.
        if "max_duration" in tokenizer.config.additional_params:
            max_duration = tokenizer._token_duration_to_ticks(
                tokenizer.config.additional_params["max_duration"],
                midi.ticks_per_quarter,
            )
            for track in midi.tracks:
                clip_durations(track.notes, max_duration)

        # Now we can sort the notes
        sort_midi(midi, sort_tracks=False)

    # For Octuple, CPWord and MMM, the time signature is carried with the notes.
    # If a MIDI doesn't have any note, no time signature will be tokenized, and in turn
    # decoded. If that's the case, we simply set time signatures to the default one.
    if (
        tokenizer.config.use_time_signatures
        and tokenization in ["Octuple", "CPWord", "MMM"]
        and (len(midi.tracks) == 0 or len(midi.tracks[0].notes) == 0)
    ):
        midi.time_signatures = [TimeSignature(0, *TIME_SIGNATURE)]


def adapt_ref_midi_for_tests_assertion(
    midi: Score, tokenizer: miditok.MIDITokenizer
) -> Score:
    """Adapt the reference tokenized MIDI so that its contents fit what is expected to
    be retrieved when decoding the tokens.
    The new MIDI will be preprocessed (`tokenizer.preprocess_midi()`), and other
    attributes such as tempos or time signature times may be altered.

    :param midi: midi reference.
    :param tokenizer: in order to downsample the MIDI before sorting its content.
    :return: a new MIDI object with track (and notes) sorted.
    """
    tokenization = type(tokenizer).__name__ if tokenizer is not None else None
    new_midi = copy(midi)

    # merging is performed in preprocess only in one_token_stream mode
    # but in multi token stream, decoding will actually keep one track per program
    if tokenizer.config.use_programs and tokenizer.one_token_stream:
        miditok.utils.merge_same_program_tracks(new_midi.tracks)

    # We delete time sigs outside of those covered by the tokenizer.
    # This is not done in ``preprocess_midi`` as the time signature alters the beat
    # structure of the music, bars will be incorrectly calculated. It is preferable
    # that to throw an error in this case.
    del_invalid_time_sig(new_midi.time_signatures, tokenizer.time_signatures)

    # Preprocess the MIDI: downsample it, remove notes outside of pitch range...
    new_midi = tokenizer.preprocess_midi(new_midi)

    # For Octuple, as tempo is only carried at notes times, we need to adapt
    # their times for comparison. Set tempo changes at onset times of notes.
    # We use the first track only, as it is the one for which tempos are decoded
    if tokenizer.config.use_tempos and tokenization in ["Octuple"]:
        if len(new_midi.tracks) > 0:
            adapt_tempo_changes_times(
                new_midi.tracks if tokenizer.one_token_stream else new_midi.tracks[:1],
                new_midi.tempos,
                tokenizer.default_tempo,
            )
        else:
            new_midi.tempos = [Tempo(0, tokenizer.default_tempo)]

    return new_midi


def midis_notes_equals(
    midi1: Score, midi2: Score
) -> list[tuple[int, str, list[tuple[str, Note | int, int]]]]:
    """Checks if the notes from two MIDIs are all equal, and if not returns the list of
    errors.

    :param midi1: first MIDI.
    :param midi2: second MIDI.
    :return: list of errors.
    """
    errors = []
    for track1, track2 in zip(midi1.tracks, midi2.tracks):
        track_errors = tracks_notes_equals(track1, track2)
        if len(track_errors) > 0:
            errors.append((track1.program, track1.name, track_errors))
    return errors


def tracks_notes_equals(
    track1: Track, track2: Track
) -> list[tuple[str, Note | int, int]]:
    if len(track1.notes) != len(track2.notes):
        return [("len", len(track2.notes), len(track1.notes))]
    errors = []
    for note1, note2 in zip(track1.notes, track2.notes):
        err = notes_equals(note1, note2)
        if err != "":
            errors.append((err, note2, getattr(note1, err)))
    return errors


def notes_equals(note1: Note, note2: Note) -> str:
    if note1.start != note2.start:
        return "start"
    elif note1.end != note2.end:
        return "end"
    elif note1.pitch != note2.pitch:
        return "pitch"
    elif note1.velocity != note2.velocity:
        return "velocity"
    return ""


def tempos_equals(tempos1: TempoTickList, tempos2: TempoTickList) -> bool:
    for tempo1, tempo2 in zip(tempos1, tempos2):
        if (
            tempo1.time != tempo2.time
            or round(tempo1.qpm, 2) != round(tempo2.qpm, 2)
            or abs(tempo1.mspq - tempo2.mspq) > 1
        ):
            return False
    return True


def check_midis_equals(
    midi1: Score,
    midi2: Score,
    check_tempos: bool = True,
    check_time_signatures: bool = True,
    check_pedals: bool = True,
    check_pitch_bends: bool = True,
    log_prefix: str = "",
) -> bool:
    has_errors = False
    types_of_errors = []

    # Checks notes and add markers if errors
    errors = midis_notes_equals(midi1, midi2)
    if len(errors) > 0:
        has_errors = True
        for e, track_err in enumerate(errors):
            if track_err[-1][0][0] != "len":
                for err, note, exp in track_err[-1]:
                    midi2.markers.append(
                        TextMeta(
                            note.start,
                            f"{e}: with note {err} (pitch {note.pitch}), expected "
                            f"{exp}",
                        )
                    )
        num_errors = sum(len(t[2]) for t in errors)
        print(f"{log_prefix} failed to encode/decode NOTES ({num_errors} errors)")

    # Check pedals
    if check_pedals:
        for inst1, inst2 in zip(midi1.tracks, midi2.tracks):
            if inst1.pedals != inst2.pedals:
                types_of_errors.append("PEDALS")
                break

    # Check pitch bends
    if check_pitch_bends:
        for inst1, inst2 in zip(midi1.tracks, midi2.tracks):
            if inst1.pitch_bends != inst2.pitch_bends:
                types_of_errors.append("PITCH BENDS")
                break

    """# Check control changes
    if check_control_changes:
        for inst1, inst2 in zip(midi1.tracks, midi2.tracks):
            if inst1.controls != inst2.controls:
                types_of_errors.append("CONTROL CHANGES")
                break"""

    # Checks tempos
    if check_tempos and not tempos_equals(midi1.tempos, midi2.tempos):
        types_of_errors.append("TEMPOS")

    # Checks time signatures
    if check_time_signatures and midi1.time_signatures != midi2.time_signatures:
        types_of_errors.append("TIME SIGNATURES")

    # Prints types of errors
    has_errors = has_errors or len(types_of_errors) > 0
    for err_type in types_of_errors:
        print(f"{log_prefix} failed to encode/decode {err_type}")

    return not has_errors


def tokenize_and_check_equals(
    midi: Score,
    tokenizer: miditok.MIDITokenizer,
    file_name: str,
) -> tuple[Score, Score, bool]:
    tokenization = type(tokenizer).__name__
    log_prefix = f"{file_name} / {tokenization}"

    # Tokenize and detokenize
    adapt_ref_midi_before_tokenize(midi, tokenizer)
    tokens = tokenizer(midi)
    midi_decoded = tokenizer(
        tokens,
        miditok.utils.get_midi_programs(midi) if len(midi.tracks) > 0 else None,
    )

    # Post-process the reference and decoded MIDIs
    midi = adapt_ref_midi_for_tests_assertion(midi, tokenizer)
    sort_midi(midi)
    sort_midi(midi_decoded)

    # Check decoded MIDI is identical
    midi_equals = check_midis_equals(
        midi,
        midi_decoded,
        check_tempos=tokenizer.config.use_tempos and tokenization != "MuMIDI",
        check_time_signatures=tokenizer.config.use_time_signatures,
        check_pedals=tokenizer.config.use_sustain_pedals,
        check_pitch_bends=tokenizer.config.use_pitch_bends,
        log_prefix=log_prefix,
    )

    # Checks types and values conformity following the rules
    err_tse = tokenizer.tokens_errors(tokens)
    if isinstance(err_tse, list):
        err_tse = sum(err_tse)
    if err_tse != 0.0:
        midi_equals = False
        print(f"{log_prefix} Validation of tokens types / values successions failed")

    return midi_decoded, midi, not midi_equals


def del_invalid_time_sig(
    time_sigs: list[TimeSignature], time_sigs_tokenizer: list[TimeSignature]
) -> None:
    r"""Will adapt the times of tempo changes depending on the
    onset times of the notes of the MIDI.
    This is needed to pass the tempo tests for Octuple as the tempos
    will be decoded only from the notes.

    :param time_sigs: time signatures to filter
    :param time_sigs_tokenizer:
    """
    idx = 0
    while idx < len(time_sigs):
        if (
            time_sigs[idx].numerator,
            time_sigs[idx].denominator,
        ) not in time_sigs_tokenizer:
            del time_sigs[idx]
        else:
            idx += 1


def adapt_tempo_changes_times(
    tracks: list[Track],
    tempo_changes: list[Tempo],
    default_tempo: int,
) -> None:
    r"""Will adapt the times of tempo changes depending on the
    onset times of the notes of the MIDI.
    This is needed to pass the tempo tests for Octuple as the tempos
    will be decoded only from the notes.

    :param tracks: tracks of the MIDI to adapt the tempo changes
    :param tempo_changes: tempo changes to adapt
    :param default_tempo: default tempo value to mock at beginning if needed
    """
    times = []
    for track in tracks:
        times += [note.time for note in track.notes]
    times.sort()

    # Fixes the first tempo at the time of the first note and mock if needed
    if round(tempo_changes[0].tempo, 2) == default_tempo:
        tempo_changes[0].time = 0
    # In case the first tempo is not the default one and occurs before the first time
    # we need to shift it and mock with the default tempo value (done below)
    elif tempo_changes[0].time < times[0]:
        tempo_changes[0].time = times[0]
    if tempo_changes[0].time != 0:
        tempo_changes.insert(0, Tempo(0, default_tempo))

    time_idx = tempo_idx = 0
    while tempo_idx < len(tempo_changes):
        # Delete tempos after the last note
        if tempo_changes[tempo_idx].time > times[-1]:
            del tempo_changes[tempo_idx]
            continue
        # Loop over incoming notes to adapt times
        # Except for the first one which is at 0
        if tempo_idx > 0:
            for n, time in enumerate(times[time_idx:]):
                if time >= tempo_changes[tempo_idx].time:
                    tempo_changes[tempo_idx].time = time
                    time_idx += n
                    break
        # Delete successive tempos at the same position (keep the latest)
        if (
            tempo_idx > 0
            and tempo_changes[tempo_idx].time == tempo_changes[tempo_idx - 1].time
        ):
            del tempo_changes[tempo_idx - 1]
            continue
        tempo_idx += 1


def clip_durations(
    notes_pedals: list[Note] | list[Pedal],
    max_duration: int,
) -> None:
    """Adapt notes and pedals offset times so that they match the possible durations
    covered by a tokenizer.

    :param notes_pedals: list of Note or Pedal objects to adapt.
    :param max_duration: max_duration in ticks
    """
    for note_pedal in notes_pedals:
        if note_pedal.duration > max_duration:
            note_pedal.duration = max_duration
