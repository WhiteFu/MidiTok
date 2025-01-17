from __future__ import annotations

import warnings

import numpy as np
from symusic import Note, Score, Tempo, TimeSignature, Track

from ..classes import Event, TokSequence
from ..constants import MIDI_INSTRUMENTS, TIME_SIGNATURE
from ..midi_tokenizer import MIDITokenizer

_ADD_TOK_ATTRIBUTES = [
    "use_programs",
    "use_chords",
    "use_rests",
    "use_tempos",
    "use_time_signatures",
]


class CPWord(MIDITokenizer):
    r"""Introduced with the
    `Compound Word Transformer (Hsiao et al.) <https://ojs.aaai.org/index.php/AAAI/article/view/16091>`_,
    this tokenization is similar to :ref:`REMI` but uses embedding pooling operations
    to reduce the overall sequence length: note tokens (*Pitch*, *Velocity* and
    *Duration*) are first independently converted to embeddings which are then merged
    (pooled) into a single one.
    Each compound token will be a list of the form (index: Token type):
    * 0: Family;
    * 1: Bar/Position;
    * 2: Pitch;
    * 3: Velocity;
    * 4: Duration;
    * (+ Optional) Program: associated with notes (pitch/velocity/duration) or chords;
    * (+ Optional) Chord: chords occurring with position tokens;
    * (+ Optional) Rest: rest acting as a TimeShift token;
    * (+ Optional) Tempo: occurring with position tokens;
    * (+ Optional) TimeSig: occurring with bar tokens.

    The output hidden states of the model will then be fed to several output layers
    (one per token type). This means that the training requires to add multiple losses.
    For generation, the decoding implies sample from several distributions, which can
    be very delicate. Hence, we do not recommend this tokenization for generation with
    small models.
    **Note:** When decoding multiple token sequences (of multiple tracks), i.e. when
    ``config.use_programs`` is False, only the tempos and time signatures of the first
    sequence will be decoded for the whole MIDI.
    """

    def _tweak_config_before_creating_voc(self) -> None:
        if self.config.use_time_signatures and self.config.use_rests:
            # NOTE: this configuration could work by adding a Bar token with the new
            # TimeSig after the Rest, but the decoding should handle this to not add
            # another bar. Or it could work by making Rests not crossing new bars.
            # Rests would have a maximal value corresponding to the difference between
            # the previous event tick and the tick of the next bar. However, in cases
            # of long rests of more than one bar, we would have successions of
            # Rest --> Bar --> Rest --> Bar ... tokens.
            warnings.warn(
                "You are using both Time Signatures and Rests with CPWord. Be aware"
                "that this configuration can result in altered time, as the time"
                "signature is carried by the Bar tokens, that are skipped during"
                "rests. To disable this warning, you can disable either Time"
                "Signatures or Rests. Otherwise, you can check that your data does"
                "not have time signature changes occurring during rests.",
                stacklevel=2,
            )
        self.config.use_sustain_pedals = False
        self.config.use_pitch_bends = False
        self.config.use_pitch_intervals = False
        self.config.program_changes = False
        token_types = ["Family", "Position", "Pitch", "Velocity", "Duration"]
        for add_tok_attr, add_token in [
            ("use_programs", "Program"),
            ("use_chords", "Chord"),
            ("use_rests", "Rest"),
            ("use_tempos", "Tempo"),
            ("use_time_signatures", "TimeSig"),
        ]:
            if getattr(self.config, add_tok_attr):
                token_types.append(add_token)
        self.vocab_types_idx = {
            type_: idx for idx, type_ in enumerate(token_types)
        }  # used for data augmentation
        self.vocab_types_idx["Bar"] = 1  # same as position

    def _add_time_events(self, events: list[Event]) -> list[list[Event]]:
        r"""Internal method intended to be implemented by inheriting classes.
        It creates the time events from the list of global and track events, and as
        such the final token sequence.

        :param events: note events to complete.
        :return: the same events, with time events inserted.
        """
        # Add time events
        all_events = []
        current_bar = -1
        bar_at_last_ts_change = 0
        previous_tick = -1
        previous_note_end = 0
        tick_at_last_ts_change = tick_at_current_bar = 0
        current_time_sig = TIME_SIGNATURE
        if self.config.log_tempos:
            # pick the closest to the default value
            current_tempo = float(
                self.tempos[(np.abs(self.tempos - self.default_tempo)).argmin()]
            )
        else:
            current_tempo = self.default_tempo
        current_program = None
        ticks_per_bar = self._compute_ticks_per_bar(
            TimeSignature(0, *current_time_sig), self.time_division
        )
        # First look for a TimeSig token, if any is given at tick 0, to update
        # current_time_sig
        if self.config.use_time_signatures:
            for event in events:
                if event.type_ == "TimeSig":
                    current_time_sig = list(map(int, event.value.split("/")))
                    ticks_per_bar = self._compute_ticks_per_bar(
                        TimeSignature(event.time, *current_time_sig),
                        self.time_division,
                    )
                    break
                elif event.type_ in [
                    "Pitch",
                    "Velocity",
                    "Duration",
                    "PitchBend",
                    "Pedal",
                ]:
                    break
        # Then look for a Tempo token, if any is given at tick 0, to update
        # current_tempo
        if self.config.use_tempos:
            for event in events:
                if event.type_ == "Tempo":
                    current_tempo = event.value
                    break
                elif event.type_ in [
                    "Pitch",
                    "Velocity",
                    "Duration",
                    "PitchBend",
                    "Pedal",
                ]:
                    break
        # Add the time events
        for e, event in enumerate(events):
            if event.type_ == "Tempo":
                current_tempo = event.value
            elif event.type_ == "Program":
                current_program = event.value
                continue
            if event.time != previous_tick:
                # (Rest)
                if (
                    self.config.use_rests
                    and event.time - previous_note_end >= self._min_rest
                ):
                    previous_tick = previous_note_end
                    rest_values = self._ticks_to_duration_tokens(
                        event.time - previous_tick, rest=True
                    )
                    # Add Rest events and increment previous_tick
                    for dur_value, dur_ticks in zip(*rest_values):
                        all_events.append(
                            self.__create_cp_token(
                                previous_tick,
                                rest=".".join(map(str, dur_value)),
                                desc=f"{event.time - previous_tick} ticks",
                            )
                        )
                        previous_tick += dur_ticks
                    # We update current_bar and tick_at_current_bar here without
                    # creating Bar tokens
                    real_current_bar = (
                        bar_at_last_ts_change
                        + (previous_tick - tick_at_last_ts_change) // ticks_per_bar
                    )
                    if real_current_bar > current_bar:
                        # In case we instantly begin with a Rest,
                        # we need to update current_bar
                        if current_bar == -1:
                            current_bar = 0
                        tick_at_current_bar += (
                            real_current_bar - current_bar
                        ) * ticks_per_bar
                        current_bar = real_current_bar

                # Bar
                num_new_bars = (
                    bar_at_last_ts_change
                    + (event.time - tick_at_last_ts_change) // ticks_per_bar
                    - current_bar
                )
                if num_new_bars >= 1:
                    if self.config.use_time_signatures:
                        time_sig_arg = f"{current_time_sig[0]}/{current_time_sig[1]}"
                    else:
                        time_sig_arg = None
                    for i in range(num_new_bars):
                        # exception when last bar and event.type == "TimeSig"
                        if i == num_new_bars - 1 and event.type_ == "TimeSig":
                            time_sig_arg = list(map(int, event.value.split("/")))
                            time_sig_arg = f"{time_sig_arg[0]}/{time_sig_arg[1]}"
                        all_events.append(
                            self.__create_cp_token(
                                (current_bar + i + 1) * ticks_per_bar,
                                bar=True,
                                desc="Bar",
                                time_signature=time_sig_arg,
                            )
                        )
                    current_bar += num_new_bars
                    tick_at_current_bar = (
                        tick_at_last_ts_change
                        + (current_bar - bar_at_last_ts_change) * ticks_per_bar
                    )

                # Position
                if event.type_ != "TimeSig":
                    pos_index = event.time - tick_at_current_bar
                    all_events.append(
                        self.__create_cp_token(
                            event.time,
                            pos=pos_index,
                            chord=event.value if event.type_ == "Chord" else None,
                            tempo=current_tempo if self.config.use_tempos else None,
                            desc="Position",
                        )
                    )

                previous_tick = event.time

            # Update time signature time variables, after adjusting the time (above)
            if event.type_ == "TimeSig":
                current_time_sig = list(map(int, event.value.split("/")))
                bar_at_last_ts_change += (
                    event.time - tick_at_last_ts_change
                ) // ticks_per_bar
                tick_at_last_ts_change = event.time
                ticks_per_bar = self._compute_ticks_per_bar(
                    TimeSignature(event.time, *current_time_sig), self.time_division
                )
                # We decrease the previous tick so that a Position token is enforced
                # for the next event
                previous_tick -= 1

            # Convert event to CP Event
            # Update max offset time of the notes encountered
            if event.type_ == "Pitch" and e + 2 < len(events):
                all_events.append(
                    self.__create_cp_token(
                        event.time,
                        pitch=event.value,
                        vel=events[e + 1].value,
                        dur=events[e + 2].value,
                        program=current_program,
                    )
                )
                previous_note_end = max(previous_note_end, event.desc)
            elif event.type_ in [
                "Program",
                "Tempo",
                "TimeSig",
                "Chord",
            ]:
                previous_note_end = max(previous_note_end, event.time)

        return all_events

    def __create_cp_token(
        self,
        time: int,
        bar: bool = False,
        pos: int | None = None,
        pitch: int | None = None,
        vel: int | None = None,
        dur: str | None = None,
        chord: str | None = None,
        rest: str | None = None,
        tempo: float | None = None,
        time_signature: str | None = None,
        program: int | None = None,
        desc: str = "",
    ) -> list[Event]:
        r"""Create a CP Word token, with the following structure:
            (index. Token type)
            0. Family
            1. Bar/Position
            2. Pitch
            3. Velocity
            4. Duration
            (5. Program) optional, with notes (pitch/velocity/duration) or chords
            (6. Chord) optional, chords occurring with position tokens
            (7. Rest) optional, rest acting as a TimeShift token
            (8. Tempo) optional, occurring with position tokens
            (9. TimeSig) optional, occurring with bar tokens
        NOTE: the first Family token (first in list) will be given as an Event object
        to keep track of time easily so that other method can sort CP tokens
        afterward.

        :param time: the current tick
        :param bar: True if this token represents a new bar occurring
        :param pos: the position index
        :param pitch: note pitch
        :param vel: note velocity
        :param dur: note duration
        :param chord: chord value
        :param rest: rest value
        :param tempo: tempo index
        :param program: a program number if you want to produce a Program CP token
            (read note above)
        :param desc: an optional argument for debug and used to spot position tokens
            in track_to_tokens
        :return: The compound token as a list of integers
        """

        def create_event(type_: str, value: str | int) -> Event:
            return Event(type_=type_, value=value, time=time, desc=desc)

        cp_token = [
            Event(type_="Family", value="Metric", time=time, desc=desc),
            Event(type_="Ignore", value="None", time=time, desc=desc),
            Event(type_="Ignore", value="None", time=time, desc=desc),
            Event(type_="Ignore", value="None", time=time, desc=desc),
            Event(type_="Ignore", value="None", time=time, desc=desc),
        ]
        cp_token += [
            create_event("Ignore", "None")
            for add_tok_attr in _ADD_TOK_ATTRIBUTES
            if getattr(self.config, add_tok_attr)
        ]

        if bar:
            cp_token[1] = create_event("Bar", "None")
            if time_signature is not None:
                cp_token[self.vocab_types_idx["TimeSig"]] = create_event(
                    "TimeSig", time_signature
                )
        elif pos is not None:
            cp_token[1] = create_event("Position", pos)
            if chord is not None:
                cp_token[self.vocab_types_idx["Chord"]] = create_event("Chord", chord)
            if tempo is not None:
                cp_token[self.vocab_types_idx["Tempo"]] = create_event(
                    "Tempo", str(tempo)
                )
        elif rest is not None:
            cp_token[self.vocab_types_idx["Rest"]] = create_event("Rest", rest)
        elif pitch is not None:
            cp_token[0].value = "Note"
            cp_token[2] = create_event("Pitch", pitch)
            cp_token[3] = create_event("Velocity", vel)
            cp_token[4] = create_event("Duration", dur)
            if program is not None:
                cp_token[self.vocab_types_idx["Program"]] = create_event(
                    "Program", program
                )

        return cp_token

    def _tokens_to_midi(
        self,
        tokens: TokSequence | list[TokSequence],
        programs: list[tuple[int, bool]] | None = None,
        time_division: int | None = None,
    ) -> Score:
        r"""Converts tokens (:class:`miditok.TokSequence`) into a MIDI and saves it.

        :param tokens: tokens to convert. Can be either a list of
            :class:`miditok.TokSequence`,
        :param programs: programs of the tracks. If none is given, will default to
            piano, program 0. (default: None)
        :param time_division: MIDI time division / resolution, in ticks/beat (of the
            MIDI to create).
        :return: the midi object (:class:`miditoolkit.MidiFile`).
        """
        if time_division is None:
            time_division = self.time_division
        # Unsqueeze tokens in case of one_token_stream
        if self.one_token_stream:  # ie single token seq
            tokens = [tokens]
        for i in range(len(tokens)):
            tokens[i] = tokens[i].tokens
        midi = Score(time_division)
        if time_division % max(self.config.beat_res.values()) != 0:
            raise ValueError(
                f"Invalid time division, please give one divisible by"
                f"{max(self.config.beat_res.values())}"
            )
        ticks_per_sample = time_division // max(self.config.beat_res.values())

        # RESULTS
        tracks: dict[int, Track] = {}
        tempo_changes = [Tempo(-1, self.default_tempo)]
        time_signature_changes = []
        tempo_changes[0].tempo = -1

        def check_inst(prog: int) -> None:
            if prog not in tracks:
                tracks[prog] = Track(
                    program=0 if prog == -1 else prog,
                    is_drum=prog == -1,
                    name="Drums" if prog == -1 else MIDI_INSTRUMENTS[prog]["name"],
                )

        current_tick = tick_at_last_ts_change = tick_at_current_bar = 0
        current_bar = -1
        bar_at_last_ts_change = 0
        current_program = 0
        current_instrument = None
        previous_note_end = 0
        for si, seq in enumerate(tokens):
            # First look for the first time signature if needed
            if si == 0:
                if self.config.use_time_signatures:
                    for compound_token in seq:
                        token_family = compound_token[0].split("_")[1]
                        if token_family == "Metric":
                            bar_pos = compound_token[1].split("_")[0]
                            if bar_pos == "Bar":
                                num, den = self._parse_token_time_signature(
                                    compound_token[
                                        self.vocab_types_idx["TimeSig"]
                                    ].split("_")[1]
                                )
                                time_signature_changes.append(
                                    TimeSignature(0, num, den)
                                )
                                break
                        else:
                            break
                if len(time_signature_changes) == 0:
                    time_signature_changes.append(TimeSignature(0, *TIME_SIGNATURE))
            current_time_sig = time_signature_changes[0]
            ticks_per_bar = self._compute_ticks_per_bar(current_time_sig, time_division)
            # Set track / sequence program if needed
            if not self.one_token_stream:
                current_tick = tick_at_last_ts_change = tick_at_current_bar = 0
                current_bar = -1
                bar_at_last_ts_change = 0
                previous_note_end = 0
                is_drum = False
                if programs is not None:
                    current_program, is_drum = programs[si]
                current_instrument = Track(
                    program=current_program,
                    is_drum=is_drum,
                    name="Drums"
                    if current_program == -1
                    else MIDI_INSTRUMENTS[current_program]["name"],
                )

            # Decode tokens
            for compound_token in seq:
                token_family = compound_token[0].split("_")[1]
                if token_family == "Note":
                    pad_range_idx = 6 if self.config.use_programs else 5
                    if any(
                        tok.split("_")[1] == "None"
                        for tok in compound_token[2:pad_range_idx]
                    ):
                        continue
                    pitch = int(compound_token[2].split("_")[1])
                    vel = int(compound_token[3].split("_")[1])
                    duration = self._token_duration_to_ticks(
                        compound_token[4].split("_")[1], time_division
                    )
                    if self.config.use_programs:
                        current_program = int(compound_token[5].split("_")[1])
                    new_note = Note(current_tick, duration, pitch, vel)
                    if self.one_token_stream:
                        check_inst(current_program)
                        tracks[current_program].notes.append(new_note)
                    else:
                        current_instrument.notes.append(new_note)
                    previous_note_end = max(previous_note_end, current_tick + duration)

                elif token_family == "Metric":
                    bar_pos = compound_token[1].split("_")[0]
                    if bar_pos == "Bar":
                        current_bar += 1
                        if current_bar > 0:
                            current_tick = tick_at_current_bar + ticks_per_bar
                        tick_at_current_bar = current_tick
                        # Add new TS only if different from the last one
                        if self.config.use_time_signatures:
                            num, den = self._parse_token_time_signature(
                                compound_token[self.vocab_types_idx["TimeSig"]].split(
                                    "_"
                                )[1]
                            )
                            if (
                                num != current_time_sig.numerator
                                or den != current_time_sig.denominator
                            ):
                                current_time_sig = TimeSignature(current_tick, num, den)
                                if si == 0:
                                    time_signature_changes.append(current_time_sig)
                                tick_at_last_ts_change = tick_at_current_bar
                                bar_at_last_ts_change = current_bar
                                ticks_per_bar = self._compute_ticks_per_bar(
                                    current_time_sig, time_division
                                )
                    elif bar_pos == "Position":  # i.e. its a position
                        if current_bar == -1:
                            # in case this Position token comes before any Bar token
                            current_bar = 0
                        current_tick = (
                            tick_at_current_bar
                            + int(compound_token[1].split("_")[1]) * ticks_per_sample
                        )
                        # Add new tempo change only if different from the last one
                        if self.config.use_tempos and si == 0:
                            tempo = float(
                                compound_token[self.vocab_types_idx["Tempo"]].split(
                                    "_"
                                )[1]
                            )
                            if (
                                tempo != round(tempo_changes[-1].tempo, 2)
                                and current_tick != tempo_changes[-1].time
                            ):
                                tempo_changes.append(Tempo(current_tick, tempo))
                    elif (
                        self.config.use_rests
                        and compound_token[self.vocab_types_idx["Rest"]].split("_")[1]
                        != "None"
                    ):
                        current_tick = max(previous_note_end, current_tick)
                        current_tick += self._token_duration_to_ticks(
                            compound_token[self.vocab_types_idx["Rest"]].split("_")[1],
                            time_division,
                        )
                        real_current_bar = (
                            bar_at_last_ts_change
                            + (current_tick - tick_at_last_ts_change) // ticks_per_bar
                        )
                        if real_current_bar > current_bar:
                            # In case we instantly begin with a Rest,
                            # we need to update current_bar
                            if current_bar == -1:
                                current_bar = 0
                            tick_at_current_bar += (
                                real_current_bar - current_bar
                            ) * ticks_per_bar
                            current_bar = real_current_bar

                    previous_note_end = max(previous_note_end, current_tick)

            # Add current_inst to midi and handle notes still active
            if not self.one_token_stream:
                midi.tracks.append(current_instrument)

        # Delete mocked
        # And handle first tempo (tick 0) here instead of super
        del tempo_changes[0]
        if len(tempo_changes) == 0 or (
            tempo_changes[0].time != 0
            and round(tempo_changes[0].tempo, 2) != self.default_tempo
        ):
            tempo_changes.insert(0, Tempo(0, self.default_tempo))
        elif round(tempo_changes[0].tempo, 2) == self.default_tempo:
            tempo_changes[0].time = 0

        # create MidiFile
        if self.one_token_stream:
            midi.tracks = list(tracks.values())
        midi.tempos = tempo_changes
        midi.time_signatures = time_signature_changes

        return midi

    def _create_base_vocabulary(self) -> list[list[str]]:
        r"""Creates the vocabulary, as a list of string tokens.
        Each token as to be given as the form of "Type_Value", separated with an
        underscore. Example: Pitch_58
        The :class:`miditok.MIDITokenizer` main class will then create the "real"
        vocabulary as a dictionary.
        Special tokens have to be given when creating the tokenizer, and
        will be added to the vocabulary by :class:`miditok.MIDITokenizer`.

        :return: the vocabulary as a list of string.
        """
        vocab = [[] for _ in range(5)]

        vocab[0].append("Family_Metric")
        vocab[0].append("Family_Note")

        # POSITION
        max_num_beats = max(ts[0] for ts in self.time_signatures)
        num_positions = max(self.config.beat_res.values()) * max_num_beats
        vocab[1].append("Ignore_None")
        vocab[1].append("Bar_None")
        vocab[1] += [f"Position_{i}" for i in range(num_positions)]

        # PITCH
        vocab[2].append("Ignore_None")
        vocab[2] += [f"Pitch_{i}" for i in range(*self.config.pitch_range)]

        # VELOCITY
        vocab[3].append("Ignore_None")
        vocab[3] += [f"Velocity_{i}" for i in self.velocities]

        # DURATION
        vocab[4].append("Ignore_None")
        vocab[4] += [
            f'Duration_{".".join(map(str, duration))}' for duration in self.durations
        ]

        # PROGRAM
        if self.config.use_programs:
            vocab += [
                ["Ignore_None"]
                + [f"Program_{program}" for program in self.config.programs]
            ]

        # CHORD
        if self.config.use_chords:
            vocab.append(["Ignore_None", *self._create_chords_tokens()])

        # REST
        if self.config.use_rests:
            vocab += [
                ["Ignore_None"]
                + [f'Rest_{".".join(map(str, rest))}' for rest in self.rests]
            ]

        # TEMPO
        if self.config.use_tempos:
            vocab += [["Ignore_None"] + [f"Tempo_{i}" for i in self.tempos]]

        # TIME_SIGNATURE
        if self.config.use_time_signatures:
            vocab += [
                ["Ignore_None"]
                + [f"TimeSig_{i[0]}/{i[1]}" for i in self.time_signatures]
            ]

        return vocab

    def _create_token_types_graph(self) -> dict[str, list[str]]:
        r"""Returns a graph (as a dictionary) of the possible token types successions.
        As with CP the tokens types are "merged", each state here corresponds to
        a "compound" token, which is characterized by the token types Program, Bar,
        Position/Chord/Tempo and Pitch/Velocity/Duration
        Here the combination of Pitch, Velocity and Duration tokens is represented by
        "Pitch" in the graph.
        NOTE: Program type is not referenced here, you can add it manually by
        modifying the tokens_types_graph class attribute following your strategy.

        :return: the token types transitions dictionary
        """
        dic = {
            "Bar": ["Position", "Bar"],
            "Position": ["Pitch"],
            "Pitch": ["Pitch", "Bar", "Position"],
        }

        if self.config.use_chords:
            dic["Rest"] = ["Rest", "Position"]
            dic["Pitch"] += ["Rest"]

        if self.config.use_rests:
            dic["Rest"] = ["Rest", "Position", "Bar"]
            dic["Pitch"] += ["Rest"]

        if self.config.use_tempos:
            # Because a tempo change can happen at any moment
            dic["Position"] += ["Position", "Bar"]
            if self.config.use_rests:
                dic["Position"].append("Rest")
                dic["Rest"].append("Position")

        for key in dic:
            dic[key].append("Ignore")
        dic["Ignore"] = list(dic.keys())

        return dic

    def _tokens_errors(self, tokens: list[list[str]]) -> int:
        r"""Checks if a sequence of tokens is made of good token types successions and
        returns the error ratio (lower is better). This method receives a list of
        tokens as a list of strings, and returns the absolute number of errors
        predicted. The number of errors should not be higher than the number of tokens.
        The Pitch and Position values are analyzed:
            - a position token cannot have a value <= to the current position (it would
                go back in time)
            - a pitch token should not be present if the same pitch is already played
                at the current position.

        :param tokens: sequence of tokens string to check.
        :return: the number of errors predicted (no more than one per token).
        """

        def cp_token_type(tok: list[str]) -> list[str]:
            family = tok[0].split("_")[1]
            if family == "Note":
                return tok[2].split("_")
            elif family == "Metric":
                bar_pos = tok[1].split("_")
                if bar_pos[0] in ["Bar", "Position"]:
                    return bar_pos
                else:  # additional token
                    for i in range(1, 5):
                        decoded_token = tok[-i].split("_")
                        if decoded_token[0] != "Ignore":
                            return decoded_token
                raise RuntimeError("No token type found, unknown error")
            elif family == "None":
                return ["PAD", "None"]
            else:  # Program
                raise RuntimeError("No token type found, unknown error")

        err = 0
        previous_type = cp_token_type(tokens[0])[0]
        current_pos = -1
        program = 0
        current_pitches = {p: [] for p in self.config.programs}

        for token in tokens[1:]:
            token_type, token_value = cp_token_type(token)
            # Good token type
            if token_type in self.tokens_types_graph[previous_type]:
                if token_type == "Bar":
                    current_pos = -1
                    current_pitches = {p: [] for p in self.config.programs}
                elif self.config.remove_duplicated_notes and token_type == "Pitch":
                    if self.config.use_programs:
                        program = int(self[5, token[5]].split("_")[1])
                    if int(token_value) in current_pitches[program]:
                        err += 1  # pitch already played at current position
                    else:
                        current_pitches[program].append(int(token_value))
                elif token_type == "Position":
                    if int(token_value) <= current_pos and previous_type != "Rest":
                        err += 1  # token position value <= to the current position
                    else:
                        current_pos = int(token_value)
                        current_pitches = {p: [] for p in self.config.programs}
            # Bad token type
            else:
                err += 1
            previous_type = token_type

        return err
