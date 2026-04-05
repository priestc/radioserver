//
//  SmartHomeWidgetLiveActivity.swift
//  SmartHomeWidget
//
//  Created by chris priest on 3/30/26.
//

import ActivityKit
import WidgetKit
import SwiftUI

struct SmartHomeWidgetAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        // Dynamic stateful properties about your activity go here!
        var emoji: String
    }

    // Fixed non-changing properties about your activity go here!
    var name: String
}

struct SmartHomeWidgetLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SmartHomeWidgetAttributes.self) { context in
            // Lock screen/banner UI goes here
            VStack {
                Text("Hello \(context.state.emoji)")
            }
            .activityBackgroundTint(Color.cyan)
            .activitySystemActionForegroundColor(Color.black)

        } dynamicIsland: { context in
            DynamicIsland {
                // Expanded UI goes here.  Compose the expanded UI through
                // various regions, like leading/trailing/center/bottom
                DynamicIslandExpandedRegion(.leading) {
                    Text("Leading")
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text("Trailing")
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text("Bottom \(context.state.emoji)")
                    // more content
                }
            } compactLeading: {
                Text("L")
            } compactTrailing: {
                Text("T \(context.state.emoji)")
            } minimal: {
                Text(context.state.emoji)
            }
            .widgetURL(URL(string: "http://www.apple.com"))
            .keylineTint(Color.red)
        }
    }
}

extension SmartHomeWidgetAttributes {
    fileprivate static var preview: SmartHomeWidgetAttributes {
        SmartHomeWidgetAttributes(name: "World")
    }
}

extension SmartHomeWidgetAttributes.ContentState {
    fileprivate static var smiley: SmartHomeWidgetAttributes.ContentState {
        SmartHomeWidgetAttributes.ContentState(emoji: "😀")
     }
     
     fileprivate static var starEyes: SmartHomeWidgetAttributes.ContentState {
         SmartHomeWidgetAttributes.ContentState(emoji: "🤩")
     }
}

#Preview("Notification", as: .content, using: SmartHomeWidgetAttributes.preview) {
   SmartHomeWidgetLiveActivity()
} contentStates: {
    SmartHomeWidgetAttributes.ContentState.smiley
    SmartHomeWidgetAttributes.ContentState.starEyes
}
