import React from 'react';
import {Link, Route} from "react-router-dom";
import OldPaginator, {
    changePageHistory,
    DEFAULT_LIMIT,
    defaultSearchOrder,
    defaultVideoOrder,
    humanFileSize,
    PageContainer,
    scrollToTop,
    searchOrders,
    secondsToString,
    TabLinks,
    VideoCards,
    videoOrders,
    WROLModeMessage
} from "./Common"
import VideoPage from "./VideoPlayer";
import {download, getChannel, getStatistics, getVideo, refresh, searchVideos} from "../api";
import {
    Button,
    Dropdown,
    Form,
    Grid,
    Header,
    Icon,
    Input,
    Loader,
    Modal,
    Radio,
    Segment,
    Statistic
} from "semantic-ui-react";
import * as QueryString from 'query-string';
import Container from "semantic-ui-react/dist/commonjs/elements/Container";
import {Channels, EditChannel, NewChannel} from "./Channels";
import {VideoPlaceholder} from "./Placeholder";

class ManageVideos extends React.Component {

    constructor(props) {
        super(props);
        this.state = {
            streamUrl: null,
            mostRecentDownload: '',
        }
    }

    download = async (e) => {
        e.preventDefault();
        let response = await download();
        if (response.stream_url) {
            this.setState({streamUrl: response.stream_url});
        }
    }

    refresh = async (e) => {
        e.preventDefault();
        let response = await refresh();
        if (response.stream_url) {
            this.setState({streamUrl: response.stream_url});
        }
    }

    render() {
        return (
            <Container fluid>
                <WROLModeMessage content='Cannot modify Videos'/>

                <p>
                    <Button secondary
                            id='refresh_videos'
                            onClick={this.refresh}
                    >
                        Refresh Video Files
                    </Button>
                    <label htmlFor='refresh_videos'>
                        Find any new video files. Remove any Videos which no longer have files.
                    </label>
                </p>
            </Container>
        )
    }
}

export class VideoWrapper extends React.Component {

    constructor(props) {
        super(props);
        this.state = {
            video: null,
            prev: null,
            next: null,
            channel: null,
            no_channel: null,
        }
    }

    async componentDidMount() {
        await this.fetchVideo();
    }

    async fetchVideo() {
        // Get and display the Video specified in the Router match
        let [video, prev, next] = await getVideo(this.props.match.params.video_id);
        let channel_id = this.props.match.params.channel_id;
        let channel = channel_id ? await getChannel(channel_id) : null;
        let no_channel = false;
        if (!channel) {
            no_channel = true;
        }
        this.setState({video, prev, next, channel, no_channel}, scrollToTop);
    }

    async componentDidUpdate(prevProps, prevState) {
        if (prevProps.match.params.video_id !== this.props.match.params.video_id) {
            // Clear the current video so that it will change, even if the video is playing.
            this.setState({video: null, prev: null, next: null, channel: null});
            await this.fetchVideo();
        }
    }

    render() {
        if (this.state.video && (this.state.no_channel || this.state.channel)) {
            return <VideoPage {...this.state} history={this.props.history} autoplay={true}/>
        } else {
            return <VideoPlaceholder/>
        }
    }
}

export function VideosPreview({videos}) {
    let body = <VideoPlaceholder/>
    if (videos && videos.length === 0) {
        body = <p>No videos available.</p>
    } else if (videos && videos.length > 0) {
        body = <VideoCards videos={videos}/>
    }
    return <>
        {body}
    </>
}

function FilterModal(props) {
    /*
    Expecting props.filters = [
        {label: 'Favorites', name: 'favorite', value: false},
        {label: 'Censored', name: 'censored', value: false},
    ];
     */
    let inputs = props.filters.map((i) =>
        <Form.Input>
            <Radio toggle
                   checked={i.value}
                   label={i.label}
                   onClick={!i.disabled ? () => props.toggleFilter(i.name) : null}
                   disabled={i.disabled}
            />
        </Form.Input>
    )

    return (
        <Modal closeIcon open={props.open} onClose={props.onClose} onOpen={props.onOpen} size='mini'>
            <Modal.Header>{props.header}</Modal.Header>
            <Modal.Content>
                <Modal.Description>
                    <Form>
                        {inputs}
                    </Form>
                </Modal.Description>
            </Modal.Content>
            <Modal.Actions>
                {props.applyButton && <Button color='blue'>Apply</Button>}
                <Button color='black' onClick={props.onClose}>Close</Button>
            </Modal.Actions>
        </Modal>
    )
}


class Videos extends React.Component {

    constructor(props) {
        super(props);
        const query = QueryString.parse(this.props.location.search);
        let activePage = query.page ? parseInt(query.page) : 1; // First page is 1 by default, of course.
        let searchStr = query.q || '';
        let searchOrder = query.o || defaultVideoOrder;
        let filtersEnabled = query.f && query.f.length > 0 ? query.f.split(',') : [];

        let filters = [
            {
                label: 'Favorites',
                name: 'favorite',
                value: filtersEnabled.indexOf('favorite') >= 0 || props.filter === 'favorite',
                disabled: props.filter === 'favorite',
            },
            {
                label: 'Censored',
                name: 'censored',
                value: filtersEnabled.indexOf('censored') >= 0
            },
        ];

        this.state = {
            activePage,
            channel: null,
            filters,
            filtersEnabled,
            filtersOpen: false,
            header: '',
            limit: DEFAULT_LIMIT,
            next: null,
            prev: null,
            queryStr: searchStr,
            searchOrder,
            searchStr: '',
            totalPages: null,
            videoOrders: searchStr === '' ? videoOrders : searchOrders,
            videos: null,
        };
    }

    async componentDidMount() {
        if (this.props.match.params.channel_id) {
            await this.fetchChannel();
        } else {
            await this.fetchVideos();
        }
        this.setHeader();
    }

    async componentDidUpdate(prevProps, prevState, snapshot) {
        let params = this.props.match.params;

        let channelChanged = params.channel_id !== prevProps.match.params.channel_id;
        let pageChanged = (
            prevState.activePage !== this.state.activePage ||
            prevState.searchOrder !== this.state.searchOrder ||
            prevState.queryStr !== this.state.queryStr
        );

        if (channelChanged) {
            await this.fetchChannel();
        } else if (pageChanged) {
            this.applyStateToHistory();
            // TODO this causes a double search request.  This will be fixed with a custom hook.
            await this.fetchVideos();
        }
    }

    setHeader() {
        let header = '';
        if (this.props.header) {
            header = this.props.header;
        } else if (this.state.channel) {
            header = this.state.channel.name;
        } else {
            // Find the matching header from the search orders.
            for (let i = 0; i < this.state.videoOrders.length; i++) {
                let item = this.state.videoOrders[i];
                if (item.value === this.state.searchOrder) {
                    header = item.title;
                }
            }
        }
        this.setState({header: header});
    }

    async fetchChannel() {
        // Get and display the channel specified in the Router match
        let channel_id = this.props.match.params.channel_id;
        let channel = null;
        if (channel_id) {
            channel = await getChannel(channel_id);
        }
        this.setState({
                channel,
                title: channel ? channel.title : '',
                offset: 0,
                total: null,
                videos: null,
                video: null,
            },
            this.fetchVideos);
    }

    enabledFilters = () => {
        let filters = this.state.filters;
        let filtersArr = [];
        for (let i = 0; i < filters.length; i++) {
            let filter = filters[i];
            if (filter['value'] === true) {
                filtersArr = filtersArr.concat([filter['name']]);
            }
        }
        return filtersArr;
    }

    async fetchVideos() {
        this.setState({videos: null});
        let offset = this.state.limit * this.state.activePage - this.state.limit;
        let channel_id = this.state.channel ? this.state.channel.id : null;
        let {queryStr, searchOrder, limit} = this.state;

        // Pass any enabled filters to the search.
        let filtersArr = this.enabledFilters();

        try {
            let [videos, total] = await searchVideos(offset, limit, channel_id, queryStr, searchOrder, filtersArr);
            let totalPages = Math.round(total / this.state.limit) || 1;
            this.setState({videos, totalPages});
        } catch (e) {
            console.error(e);
        }
    }

    changePage = async (activePage) => {
        this.setState({activePage});
    }

    clearSearch = async () => {
        this.setState({searchStr: '', queryStr: '', searchOrder: defaultVideoOrder, activePage: 1});
    }

    applyStateToHistory = () => {
        let {history, location} = this.props;
        let {activePage, queryStr, searchOrder} = this.state;
        let filters = this.enabledFilters();
        changePageHistory(history, location, activePage, queryStr, searchOrder, filters);
    }

    handleSearch = async (e) => {
        e && e.preventDefault();
        this.setState({activePage: 1, searchOrder: defaultSearchOrder, queryStr: this.state.searchStr},
            this.applyStateToHistory);
    }

    handleInputChange = (event, {name, value}) => {
        this.setState({[name]: value});
    }

    changeSearchOrder = (event, {value}) => {
        this.setState({searchOrder: value, activePage: 1}, this.applyStateToHistory);
    }

    toggleFilter = (name) => {
        let filters = this.state.filters;
        let filtersEnabled = [];
        for (let i = 0; i < filters.length; i++) {
            let filter = filters[i];
            if (filter.name === name) {
                filter.value = !filter.value;
            }
            if (filter.value) {
                filtersEnabled = filtersEnabled.concat([filter.name]);
            }
        }
        this.setState({filters, filtersEnabled});
    }

    applyFilters = async () => {
        this.applyStateToHistory();
        await this.fetchVideos();
    }

    closeFilters = () => {
        this.setState({filtersOpen: false}, this.applyFilters);
    }

    openFilters = () => {
        this.setState({filtersOpen: true});
    }

    render() {
        let {
            activePage,
            channel,
            queryStr,
            searchOrder,
            searchStr,
            header,
            totalPages,
            videoOrders,
            videos,
            filtersEnabled,
        } = this.state;

        let body = <Container fluid style={{marginTop: '1em'}}><VideoPlaceholder/></Container>;

        if (videos && videos.length === 0) {
            // API didn't send back any videos, tell the user what to do.
            if (this.props.filter === 'favorites') {
                body = <p>You haven't tagged any videos as favorite.</p>;
            } else {
                // default empty body.
                body = <p>No videos retrieved. Have you downloaded videos yet?</p>;
            }
        } else if (videos) {
            body = <VideoCards videos={videos}/>;
        }

        let pagination = null;
        if (totalPages) {
            pagination = (
                <div style={{marginTop: '3em', textAlign: 'center'}}>
                    <OldPaginator
                        activePage={activePage}
                        changePage={this.changePage}
                        totalPages={totalPages}
                    />
                </div>
            );
        }

        let clearSearchButton = (
            <Button icon labelPosition='right' onClick={this.clearSearch}>
                Search: {queryStr}
                <Icon name='close'/>
            </Button>
        );

        return (
            <Container fluid>
                <Grid columns={4} stackable>
                    <Grid.Column textAlign='left' width={6}>
                        <h1>
                            {header}
                            {
                                channel &&
                                <>
                                    &nbsp;
                                    &nbsp;
                                    <Link to={`/videos/channel/${channel.id}/edit`}>
                                        <Icon name="edit"/>
                                    </Link>
                                </>
                            }
                        </h1>
                        {queryStr && clearSearchButton}
                    </Grid.Column>
                    <Grid.Column width={1}>
                        <Button icon onClick={this.openFilters} color={filtersEnabled.length > 0 ? 'black' : null}>
                            <Icon name='filter'/>
                        </Button>
                        <FilterModal
                            applyFilters={this.applyFilters}
                            open={this.state.filtersOpen}
                            onClose={this.closeFilters}
                            header='Filter Videos'
                            filters={this.state.filters}
                            toggleFilter={this.toggleFilter}
                            applyButton={null}
                        />
                    </Grid.Column>
                    <Grid.Column textAlign='right' width={5}>
                        <Form onSubmit={this.handleSearch}>
                            <Input
                                fluid
                                icon='search'
                                placeholder='Search...'
                                name="searchStr"
                                value={searchStr}
                                onChange={this.handleInputChange}/>
                        </Form>
                    </Grid.Column>
                    <Grid.Column width={4}>
                        <Dropdown
                            size='large'
                            placeholder='Sort by...'
                            selection
                            fluid
                            name='searchOrder'
                            onChange={this.changeSearchOrder}
                            value={searchOrder}
                            options={videoOrders}
                            disabled={searchOrder === defaultSearchOrder}
                        />
                    </Grid.Column>
                </Grid>
                <Container fluid>
                    {body}
                </Container>
                {pagination}
            </Container>
        )
    }
}

function ManageTab(props) {
    return (
        <Container fluid>
            <ManageVideos/>
            <Statistics/>
        </Container>
    )
}

export function VideosRoute(props) {

    const links = [
        {text: 'Videos', to: '/videos', exact: true, key: 'videos'},
        {text: 'Favorites', to: '/videos/favorites', key: 'favorites'},
        {text: 'Channels', to: '/videos/channel', key: 'channel'},
        {text: 'Manage', to: '/videos/manage', key: 'manage'},
    ];

    return (
        <PageContainer>
            <TabLinks links={links}/>

            <Route path='/videos' exact
                   component={(i) =>
                       <Videos
                           match={i.match}
                           history={i.history}
                           location={i.location}
                       />}
            />
            <Route path='/videos/favorites' exact
                   component={(i) =>
                       <Videos
                           match={i.match}
                           history={i.history}
                           location={i.location}
                           filter='favorite'
                           header='Favorite Videos'
                       />
                   }/>
            <Route path='/videos/channel' exact component={Channels}/>
            <Route path='/videos/manage' exact component={ManageTab}/>
            <Route path='/videos/channel/new' exact component={NewChannel}/>
            <Route path='/videos/channel/:channel_id/edit' exact
                   component={(i) =>
                       <EditChannel
                           match={i.match}
                           history={i.history}
                       />
                   }
            />
            <Route path='/videos/channel/:channel_id/video' exact component={Videos}/>
        </PageContainer>
    )
}

class Statistics extends React.Component {

    constructor(props) {
        super(props);
        this.state = {
            videos: null,
            historical: null,
            channels: null,
        };
        this.videoNames = [
            {key: 'videos', label: 'Downloaded Videos'},
            {key: 'favorites', label: 'Favorite Videos'},
            {key: 'sum_size', label: 'Total Size'},
            {key: 'max_size', label: 'Largest Video'},
            {key: 'week', label: 'Downloads Past Week'},
            {key: 'month', label: 'Downloads Past Month'},
            {key: 'year', label: 'Downloads Past Year'},
            {key: 'sum_duration', label: 'Total Duration'},
        ];
        this.historicalNames = [
            {key: 'average_count', label: 'Average Monthly Downloads'},
            {key: 'average_size', label: 'Average Monthly Usage'},
        ];
        this.channelNames = [
            {key: 'channels', label: 'Channels'},
        ];
    }

    async componentDidMount() {
        await this.fetchStatistics();
    }

    async fetchStatistics() {
        try {
            let stats = await getStatistics();
            stats.videos.sum_duration = secondsToString(stats.videos.sum_duration);
            stats.videos.sum_size = humanFileSize(stats.videos.sum_size, true);
            stats.videos.max_size = humanFileSize(stats.videos.max_size, true);
            stats.historical.average_size = humanFileSize(stats.historical.average_size, true);
            this.setState({...stats});
        } catch (e) {
            console.error(e);
        }
    }

    buildSegment(title, names, stats) {
        return <Segment secondary>
            <Header textAlign='center' as='h1'>{title}</Header>
            <Statistic.Group>
                {names.map(
                    ({key, label}) =>
                        <Statistic key={key} style={{margin: '2em'}}>
                            <Statistic.Value>{stats[key]}</Statistic.Value>
                            <Statistic.Label>{label}</Statistic.Label>
                        </Statistic>
                )}
            </Statistic.Group>
        </Segment>
    }

    render() {
        if (this.state.videos) {
            return (
                <>
                    {this.buildSegment('Videos', this.videoNames, this.state.videos)}
                    {this.buildSegment('Historical Video', this.historicalNames, this.state.historical)}
                    {this.buildSegment('Channels', this.channelNames, this.state.channels)}
                </>
            )
        } else {
            return <Loader active inline='centered'/>
        }
    }
}
