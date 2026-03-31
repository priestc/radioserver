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

        // Wire up AudioPlayer to APIService if the main app hasn't launched yet
        let player = AudioPlayer.shared
        if player.apiService == nil {
            player.apiService = APIService.shared
            player.startSyncTimer()
        }

        let nowPlaying = CPNowPlayingTemplate.shared
        nowPlaying.isUpNextButtonEnabled = true
        nowPlaying.isAlbumArtistButtonEnabled = false
        nowPlaying.add(self)

        interfaceController.setRootTemplate(nowPlaying, animated: false, completion: nil)

        observeQueue()
    }

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didDisconnect interfaceController: CPInterfaceController
    ) {
        CPNowPlayingTemplate.shared.remove(self)
        self.interfaceController = nil
        cancellables.removeAll()
    }

    // Observe queue so Up Next badge stays current
    private func observeQueue() {
        AudioPlayer.shared.$queue
            .receive(on: DispatchQueue.main)
            .sink { [weak self] queue in
                guard let self, let controller = self.interfaceController else { return }
                // If the Up Next list is currently shown, refresh it
                if let top = controller.topTemplate as? CPListTemplate, top.title == "Up Next" {
                    self.refreshQueueTemplate(top, queue: queue)
                }
            }
            .store(in: &cancellables)
    }

    private func makeQueueTemplate() -> CPListTemplate {
        let queue = AudioPlayer.shared.queue
        let template = CPListTemplate(title: "Up Next", sections: [makeQueueSection(queue)])
        template.emptyViewTitleVariants = ["Queue is empty"]
        template.emptyViewSubtitleVariants = ["Songs are loading from the server"]
        return template
    }

    private func makeQueueSection(_ queue: [SongItem]) -> CPListSection {
        let items = queue.enumerated().map { index, song in
            CPListItem(text: song.title, detailText: song.artist)
        }
        return CPListSection(items: items)
    }

    private func refreshQueueTemplate(_ template: CPListTemplate, queue: [SongItem]) {
        template.updateSections([makeQueueSection(queue)])
    }
}

extension CarPlaySceneDelegate: CPNowPlayingTemplateObserver {
    func nowPlayingTemplateUpNextButtonTapped(_ nowPlayingTemplate: CPNowPlayingTemplate) {
        guard let controller = interfaceController else { return }
        controller.pushTemplate(makeQueueTemplate(), animated: true, completion: nil)
    }

    func nowPlayingTemplateAlbumArtistButtonTapped(_ nowPlayingTemplate: CPNowPlayingTemplate) {}
}
