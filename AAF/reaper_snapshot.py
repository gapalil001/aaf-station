# @noindex
"""Read a snapshot of the active REAPER project for the export window."""
from pathlib import Path
from reaper_apply import valid_pointer


def read_project(api):
    project, _, filename, _ = api.RPR_EnumProjects(-1, "", 4096)
    time_range = api.RPR_GetSet_LoopTimeRange2(project, False, False, 0, 0, False)
    fps, _, drop = api.RPR_TimeMap_curFrameRate(project, False)
    sample_rate = api.RPR_GetSetProjectInfo(project, "PROJECT_SRATE", 0, False)
    regions = []
    for i in range(api.RPR_GetNumRegionsOrMarkers(project)):
        marker = api.RPR_GetRegionOrMarker(project, i, "")
        value = lambda key: api.RPR_GetRegionOrMarkerInfo_Value(project, marker, key)
        if value("B_ISREGION"):
            name = api.RPR_GetSetRegionOrMarkerInfo_String(project, marker, "P_NAME", "", False)[4]
            regions.append(dict(name=name, number=int(value("I_NUMBER")),
                                start=value("D_STARTPOS"), end=value("D_ENDPOS")))

    tracks = []
    for i in range(api.RPR_CountTracks(project)):
        track = api.RPR_GetTrack(project, i)
        name = api.RPR_GetTrackName(track, "", 4096)[2] or "Track %d" % (i + 1)
        tv = lambda key: api.RPR_GetMediaTrackInfo_Value(track, key)
        warnings = []
        if (api.RPR_TrackFX_GetCount(track)
                or api.RPR_CountTrackEnvelopes(track) or abs(tv("D_VOL") - 1) > 1e-9
                or abs(tv("D_PAN")) > 1e-9 or tv("B_MUTE")):
            warnings.append("Track FX, automation, fader, pan and mute are outside the AAF clip edit.")
        clips = []
        for j in range(api.RPR_GetTrackNumMediaItems(track)):
            item = api.RPR_GetTrackMediaItem(track, j)
            iv = lambda key: api.RPR_GetMediaItemInfo_Value(item, key)
            take = api.RPR_GetActiveTake(item)
            if not valid_pointer(take) or api.RPR_TakeIsMIDI(take) or iv("B_MUTE"):
                continue
            take_name = api.RPR_GetTakeName(take) or "Clip %d" % (j + 1)
            source = api.RPR_GetMediaItemTake_Source(take)
            take_value = lambda key: api.RPR_GetMediaItemTakeInfo_Value(take, key)
            error = ""
            source_path, channels, source_length = "", 0, 0
            start = take_value("D_STARTOFFS")
            if valid_pointer(source):
                source_path = api.RPR_GetMediaSourceFileName(source, "", 4096)[1]
                channels = api.RPR_GetMediaSourceNumChannels(source)
                source_length, _, is_qn = api.RPR_GetMediaSourceLength(source, False)
                source_type = api.RPR_GetMediaSourceType(source, "", 128)[1]
                if is_qn or source_type in ("SECTION", "VIDEO", "VIDEOEFFECT", "RPP_PROJECT"):
                    error = "Source needs to be glued or rendered to an audio WAV before export."
            if not source_path or channels < 1:
                error = "No readable audio source."
            if (abs(take_value("D_PLAYRATE") - 1) > 1e-9 or abs(take_value("D_PITCH")) > 1e-9
                    or api.RPR_GetTakeNumStretchMarkers(take) or iv("C_AUTOSTRETCH")):
                error = "Glue this item to commit its playback rate, pitch or stretch markers."
            if iv("B_ALLTAKESPLAY"):
                error = "Glue this item to commit Play all takes."
            notes = []
            if api.RPR_TakeFX_GetCount(take) or api.RPR_CountTakeEnvelopes(take) or abs(take_value("D_PAN")) > 1e-9:
                notes.append("Take FX, envelopes and pan are omitted.")
            fades = {}
            for kind in ("IN", "OUT"):
                length = iv("D_FADE" + kind + "LEN_AUTO")
                if length <= 0:
                    length = iv("D_FADE" + kind + "LEN")
                curved = (iv("C_FADE" + kind + "SHAPE")
                          or abs(iv("D_FADE" + kind + "DIR_NEW")) > 1e-9
                          or abs(iv("D_FADE" + kind + "DIR2_NEW")) > 1e-9)
                fades["fade_" + kind.lower()] = max(0, length)
                if length > 0 and (curved or iv("I_FADELPF")):
                    notes.append("Curved or filtered fades use linear fades in the AAF.")
            clips.append(dict(name=take_name, position=iv("D_POSITION"), duration=iv("D_LENGTH"),
                              offset=start, path=source_path, channels=channels, source_length=source_length,
                              channel_mode=int(take_value("I_CHANMODE")), gain=iv("D_VOL") * take_value("D_VOL"),
                              loop=bool(iv("B_LOOPSRC")), error=error, notes=notes, **fades))
        tracks.append(dict(id=str(i + 1), name=name, selected=bool(api.RPR_IsTrackSelected(track)),
                           clips=clips, warnings=warnings))
    project_name = Path(filename).stem if filename else "REAPER Session"
    return dict(name=project_name, path=filename, length=api.RPR_GetProjectLength(project),
                sample_rate=int(sample_rate) or 48000, timecode=api.RPR_GetProjectTimeOffset(project, False),
                fps=fps, drop=bool(drop), time_selection=list(time_range[3:5]), regions=regions, tracks=tracks)
