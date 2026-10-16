package com.vibeledger.companion

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Canvas
import android.graphics.Matrix
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayOutputStream
import java.io.FilterInputStream
import java.io.InputStream
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

object ImagePreparer {
    private const val EDGE = 1600
    private const val MAX_INPUT = 32L * 1024 * 1024
    private const val MAX_JPEG = 2 * 1024 * 1024

    private class Bounded(input: InputStream) : FilterInputStream(input) {
        private var count = 0L
        private fun add(value: Int): Int { if (value > 0) { count += value; require(count <= MAX_INPUT) }; return value }
        override fun read(): Int = super.read().also { if (it >= 0) add(1) }
        override fun read(buffer: ByteArray, offset: Int, length: Int): Int = add(`in`.read(buffer, offset, length))
        override fun skip(n: Long): Long = `in`.skip(n.coerceAtMost(MAX_INPUT - count + 1)).also {
            count += it; require(count <= MAX_INPUT)
        }
    }
    private fun open(resolver: ContentResolver, uri: Uri): InputStream {
        require(uri.scheme == "content") // No arbitrary file paths or remote URLs from share intents.
        return Bounded(requireNotNull(resolver.openInputStream(uri)))
    }

    suspend fun jpeg(resolver: ContentResolver, uri: Uri): ByteArray = withContext(Dispatchers.IO) {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        open(resolver, uri).use { BitmapFactory.decodeStream(it, null, bounds) }
        require(bounds.outWidth in 1..100_000 && bounds.outHeight in 1..100_000)
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > EDGE * 2) sample *= 2
        val options = BitmapFactory.Options().apply { inSampleSize = sample }
        val decoded = open(resolver, uri).use { requireNotNull(BitmapFactory.decodeStream(it, null, options)) }
        try {
            val exif = open(resolver, uri).use { ExifInterface(it) }
            val ratio = minOf(1f, EDGE.toFloat() / maxOf(decoded.width, decoded.height))
            val matrix = Matrix().apply {
                postScale(ratio, ratio)
                if (exif.isFlipped) postScale(-1f, 1f)
                postRotate(exif.rotationDegrees.toFloat())
            }
            val oriented = Bitmap.createBitmap(decoded, 0, 0, decoded.width, decoded.height, matrix, true)
            try {
                // Flatten alpha onto white; re-encoding removes EXIF/location metadata.
                val opaque = Bitmap.createBitmap(oriented.width, oriented.height, Bitmap.Config.ARGB_8888)
                try {
                    Canvas(opaque).apply { drawColor(Color.WHITE); drawBitmap(oriented, 0f, 0f, null) }
                    for (quality in listOf(85, 70, 55)) {
                        val output = ByteArrayOutputStream()
                        check(opaque.compress(Bitmap.CompressFormat.JPEG, quality, output))
                        if (output.size() <= MAX_JPEG) return@withContext output.toByteArray()
                    }
                    error("Image too large")
                } finally { opaque.recycle() }
            } finally { if (oriented !== decoded) oriented.recycle() }
        } finally { decoded.recycle() }
    }
}
