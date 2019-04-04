import QtQuick 2.9
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3
import QtQuick.Controls.Material 2.2
import QtGraphicalEffects 1.0

Button {
    id: button
    font.capitalization: Font.AllUppercase
    font.styleName: "Medium"
    font.pointSize: 14
    Material.foreground: button.flat ? yubicoGreen : yubicoWhite
    Material.background: button.flat ? "transparent" : yubicoGreen
    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        enabled: false
    }
}
