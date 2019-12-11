import React from 'react';
import Button from "react-bootstrap/Button";

const VIDEOS_API = '/api/videos/';

function Video(props) {
    let params = props.match.params;
    let video_hash = params.video_hash;
    let poster_url = VIDEOS_API + 'static/poster/' + video_hash;
    let video_url = VIDEOS_API + 'static/video/' + video_hash;
    let captions_url = VIDEOS_API + 'static/caption/' + video_hash;
    let video_download_url = video_url + '?download=true';

    return (
        <>
            <video poster={poster_url} id="player" playsinline controls style={{'maxWidth': '100%'}}>
                <source src={video_url} type="video/mp4"/>
                <track kind="captions" label="English captions" src={captions_url} srcLang="en" default/>
            </video>

            <a href={video_download_url}>
                <Button>Download</Button>
            </a>
        </>
    )
}

export default Video;
