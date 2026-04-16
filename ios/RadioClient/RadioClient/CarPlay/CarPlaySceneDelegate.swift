import CarPlay
import Combine

class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    var interfaceController: CPInterfaceController?
    private var cancellables = Set<AnyCancellable>()

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didConnect interfaceController: CPInterfaceController
    ) {
        self.interfaceController = interfaceController

        let player = AudioPlayer.shared
        if player.apiService == nil {
            player.apiService = APIService.shared
            player.startSyncTimer()
        }
        player.fetchChannels()

        let nowPlaying = CPNowPlayingTemplate.shared
        nowPlaying.isUpNextButtonEnabled = true
        nowPlaying.isAlbumArtistButtonEnabled = false
        nowPlaying.add(self)

        interfaceController.setRootTemplate(nowPlaying, animated: false, completion: nil)

        observeChanges()
    }

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didDisconnect interfaceController: CPInterfaceController
    ) {
        CPNowPlayingTemplate.shared.remove(self)
        self.interfaceController = nil
        cancellables.removeAll()
    }

    // Refresh the list template whenever queue, channels, or selected channel changes
    private func observeChanges() {
        AudioPlayer.shared.$queue
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.refreshListTemplateIfVisible() }
            .store(in: &cancellables)

        AudioPlayer.shared.$availableChannels
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.refreshListTemplateIfVisible() }
            .store(in: &cancellables)

        AudioPlayer.shared.$selectedChannel
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.refreshListTemplateIfVisible() }
            .store(in: &cancellables)
    }

    private func refreshListTemplateIfVisible() {
        guard let controller = interfaceController,
              let top = controller.topTemplate as? CPListTemplate,
              top.title == "Up Next" else { return }
        top.updateSections(makeAllSections())
    }

    // MARK: - Template construction

    private func makeQueueTemplate() -> CPListTemplate {
        let template = CPListTemplate(title: "Up Next", sections: makeAllSections())
        template.emptyViewTitleVariants = ["Queue is empty"]
        template.emptyViewSubtitleVariants = ["Songs are loading from the server"]
        return template
    }

    private func makeAllSections() -> [CPListSection] {
        var sections: [CPListSection] = []
        sections.append(makeChannelSection())
        let queueSection = makeQueueSection(AudioPlayer.shared.queue)
        if (queueSection.items.count) > 0 {
            sections.append(queueSection)
        }
        return sections
    }

    private func makeChannelSection() -> CPListSection {
        let player = AudioPlayer.shared
        let channels = player.availableChannels

        var items: [CPListItem] = []

        // "All Music" entry
        let isAllActive = player.selectedChannel == nil
        let allItem = CPListItem(
            text: isAllActive ? "✓ All Music" : "All Music",
            detailText: "Play everything"
        )
        allItem.handler = { [weak self] _, completion in
            AudioPlayer.shared.selectChannel(nil)
            self?.interfaceController?.popTemplate(animated: true, completion: nil)
            completion()
        }
        items.append(allItem)

        for channel in channels {
            let isActive = player.selectedChannel?.id == channel.id
            let item = CPListItem(
                text: isActive ? "✓ \(channel.name)" : channel.name,
                detailText: channel.subtitle
            )
            let captured = channel
            item.handler = { [weak self] _, completion in
                AudioPlayer.shared.selectChannel(captured)
                self?.interfaceController?.popTemplate(animated: true, completion: nil)
                completion()
            }
            items.append(item)
        }

        return CPListSection(items: items, header: "Channel", sectionIndexTitle: nil)
    }

    private func makeQueueSection(_ queue: [SongItem]) -> CPListSection {
        let items = queue.map { song in
            CPListItem(text: song.title, detailText: song.artist)
        }
        return CPListSection(items: items, header: "Up Next", sectionIndexTitle: nil)
    }
}

extension CarPlaySceneDelegate: CPNowPlayingTemplateObserver {
    func nowPlayingTemplateUpNextButtonTapped(_ nowPlayingTemplate: CPNowPlayingTemplate) {
        guard let controller = interfaceController else { return }
        controller.pushTemplate(makeQueueTemplate(), animated: true, completion: nil)
    }

    func nowPlayingTemplateAlbumArtistButtonTapped(_ nowPlayingTemplate: CPNowPlayingTemplate) {}
}
