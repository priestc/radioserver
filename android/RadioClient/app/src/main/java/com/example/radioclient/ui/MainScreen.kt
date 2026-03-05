package com.example.radioclient.ui

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.example.radioclient.RadioClientApp

sealed class Screen(val route: String, val label: String, val icon: ImageVector) {
    data object NowPlaying : Screen("now_playing", "Now Playing", Icons.Default.MusicNote)
    data object History : Screen("history", "History", Icons.Default.History)
    data object Settings : Screen("settings", "Settings", Icons.Default.Settings)
}

private val screens = listOf(Screen.NowPlaying, Screen.History, Screen.Settings)

@Composable
fun MainScreen(app: RadioClientApp) {
    val navController = rememberNavController()

    Scaffold(
        bottomBar = {
            NavigationBar {
                val navBackStackEntry by navController.currentBackStackEntryAsState()
                val currentDestination = navBackStackEntry?.destination
                screens.forEach { screen ->
                    NavigationBarItem(
                        icon = { Icon(screen.icon, contentDescription = screen.label) },
                        label = { Text(screen.label) },
                        selected = currentDestination?.hierarchy?.any {
                            it.route == screen.route
                        } == true,
                        onClick = {
                            navController.navigate(screen.route) {
                                popUpTo(navController.graph.findStartDestination().id) {
                                    saveState = true
                                }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                    )
                }
            }
        },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = Screen.NowPlaying.route,
            modifier = Modifier.padding(innerPadding),
        ) {
            composable(Screen.NowPlaying.route) {
                NowPlayingScreen(app = app)
            }
            composable(Screen.History.route) {
                HistoryScreen(app = app)
            }
            composable(Screen.Settings.route) {
                SettingsScreen(app = app)
            }
        }
    }
}
